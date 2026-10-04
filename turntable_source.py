#!/usr/bin/env python3
"""Turntable control plane for Unraid.

The Raspberry Pi owns the USB audio device. This service reads its raw ALSA
capture stream over SSH, exposes it as a live WAV stream, and lets the
Sendspin source role and Music Assistant.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import html
import io
import logging
import os
import shlex
import time
import wave
from collections import deque
from pathlib import Path
from typing import Any, AsyncIterator

from aiosendspin.client import ClientListener, PairingSupport, SendspinClient
from aiosendspin.models.source import ClientHelloSourceFeatures, ClientHelloSourceSupport
from aiosendspin.models.player import SupportedAudioFormat
from aiosendspin.models.types import AudioCodec, Roles
from aiosendspin.noise import Identity
from aiosendspin.noise.trust_store import FileClientPairingStore
import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
import uvicorn


LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format=LOG_FORMAT)
log = logging.getLogger("turntable")
logs: deque[str] = deque(maxlen=300)


class MemoryLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        logs.append(self.format(record))


memory_handler = MemoryLogHandler()
memory_handler.setFormatter(logging.Formatter(LOG_FORMAT))
logging.getLogger().addHandler(memory_handler)


class Settings:
    name = os.getenv("TURNTABLE_NAME", "Turntable")
    web_port = int(os.getenv("WEB_PORT", "8383"))
    sendspin_port = int(os.getenv("SENDSPIN_PORT", "8928"))
    pi_host = os.getenv("PI_HOST", "raspberrypi.local")
    pi_user = os.getenv("PI_USER", "pi")
    pi_audio_device = os.getenv("PI_AUDIO_DEVICE", "plughw:CARD=Device,DEV=0")
    ssh_key = os.getenv("SSH_KEY", "/config/ssh/id_rsa")
    ssh_known_hosts = os.getenv("SSH_KNOWN_HOSTS", "/config/ssh/known_hosts")
    sample_rate = int(os.getenv("AUDIO_RATE", "48000"))
    channels = int(os.getenv("AUDIO_CHANNELS", "2"))
    ma_url = os.getenv("MA_URL", "").rstrip("/")
    ma_token = os.getenv("MA_TOKEN", "")
    ma_entity = os.getenv("MA_ENTITY", "")
    audd_token = os.getenv("AUDD_TOKEN", "")
    sendspin_pairing_code = os.getenv("SENDSPIN_PAIRING_CODE", "")
    pairing_file = os.getenv("SENDSPIN_PAIRING_FILE", "/config/sendspin/pairing.json")
    identity_file = os.getenv("SENDSPIN_IDENTITY_FILE", "/config/sendspin/identity")
    auto_recognize = os.getenv("AUTO_RECOGNIZE", "true").lower() in {"1", "true", "yes", "on"}
    recognition_interval = int(os.getenv("RECOGNITION_INTERVAL", "30"))
    capture_chunk_ms = int(os.getenv("CAPTURE_CHUNK_MS", "100"))
    capture_buffer_ms = int(os.getenv("CAPTURE_BUFFER_MS", "400"))
    arecord_buffer_us = int(os.getenv("ARECORD_BUFFER_US", "500000"))
    arecord_period_us = int(os.getenv("ARECORD_PERIOD_US", "100000"))


settings = Settings()


def ssh_base() -> list[str]:
    command = [
        "ssh",
        "-i",
        settings.ssh_key,
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=8",
    ]
    if Path(settings.ssh_known_hosts).exists():
        command += ["-o", f"UserKnownHostsFile={settings.ssh_known_hosts}"]
    else:
        command += ["-o", "StrictHostKeyChecking=accept-new"]
    return command + [f"{settings.pi_user}@{settings.pi_host}"]


class PiCapture:
    def __init__(self) -> None:
        self.process: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task[None] | None = None
        self.subscribers: set[asyncio.Queue[bytes | None]] = set()
        self.last_data_at: float | None = None
        self.bytes_received = 0
        self.last_error = ""
        self.recent_chunks: deque[bytes] = deque(maxlen=400)
        self.lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def ensure_started(self) -> None:
        async with self.lock:
            if self.running:
                return
            command = ssh_base() + [
                "arecord",
                "-D",
                settings.pi_audio_device,
                "-f",
                "S16_LE",
                "-r",
                str(settings.sample_rate),
                "-c",
                str(settings.channels),
                "-t",
                "raw",
                "-B",
                str(settings.arecord_buffer_us),
                "-F",
                str(settings.arecord_period_us),
            ]
            log.info("Starting Pi capture: %s", " ".join(shlex.quote(part) for part in command))
            self.process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self.last_error = ""
            self.task = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        process = self.process
        try:
            while True:
                data = await process.stdout.read(3840)
                if not data:
                    break
                self.last_data_at = time.time()
                self.bytes_received += len(data)
                self.recent_chunks.append(data)
                for subscriber in list(self.subscribers):
                    await subscriber.put(data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = str(exc)
            log.exception("Capture reader failed")
        finally:
            if process.returncode is None:
                process.terminate()
            await process.wait()
            stderr = b""
            if process.stderr:
                with contextlib.suppress(Exception):
                    stderr = await process.stderr.read()
            if stderr:
                self.last_error = stderr.decode(errors="replace").strip()[-500:]
                log.error("Pi capture stopped: %s", self.last_error)
            self.process = None
            for subscriber in list(self.subscribers):
                with contextlib.suppress(asyncio.QueueFull):
                    subscriber.put_nowait(None)

    async def stream(self) -> AsyncIterator[bytes]:
        await self.ensure_started()
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=20)
        self.subscribers.add(queue)
        try:
            while True:
                data = await queue.get()
                if data is None:
                    return
                yield data
        finally:
            self.subscribers.discard(queue)
            if not self.subscribers:
                await self.stop()

    async def stop(self) -> None:
        async with self.lock:
            if self.task and not self.task.done():
                self.task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.task
            self.task = None
            self.process = None

    async def test(self) -> dict[str, Any]:
        command = ssh_base() + ["arecord", "-l"]
        try:
            result = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, stderr = await result.communicate()
            return {
                "ok": result.returncode == 0,
                "output": (stdout + stderr).decode(errors="replace")[-4000:],
            }
        except Exception as exc:
            return {"ok": False, "output": str(exc)}

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "pi": f"{settings.pi_user}@{settings.pi_host}",
            "device": settings.pi_audio_device,
            "bytes_received": self.bytes_received,
            "last_data_at": self.last_data_at,
            "last_error": self.last_error,
        }

    def recent_wav(self) -> bytes:
        pcm = b"".join(self.recent_chunks)
        output = io.BytesIO()
        with wave.open(output, "wb") as wav_file:
            wav_file.setnchannels(settings.channels)
            wav_file.setsampwidth(2)
            wav_file.setframerate(settings.sample_rate)
            wav_file.writeframes(pcm)
        return output.getvalue()


capture = PiCapture()
now_playing: dict[str, str] = {"title": "", "artist": "", "album": "", "source": "manual"}
pairing_code = ""
recognition_task: asyncio.Task[None] | None = None
last_recognition_key = ""


def load_identity() -> Identity:
    path = Path(settings.identity_file)
    if path.exists():
        return Identity.from_private_bytes(base64.urlsafe_b64decode(path.read_text().strip() + "=="))
    identity = Identity.generate()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(identity.private_b64u, encoding="ascii")
    path.chmod(0o600)
    return identity


class SendspinSourceBridge:
    def __init__(self) -> None:
        self.client: SendspinClient | None = None
        self.listener: ClientListener | None = None
        self.capture_task: asyncio.Task[None] | None = None
        self.source_capture: Any = None
        self.pairing_store: FileClientPairingStore | None = None

    @property
    def connected(self) -> bool:
        return bool(self.client and self.client.connected)

    async def start(self) -> None:
        self.pairing_store = await FileClientPairingStore.open(settings.pairing_file)
        if settings.sendspin_pairing_code:
            await self.pairing_store.set_static_pairing_code(settings.sendspin_pairing_code)
        self.listener = ClientListener(
            client_id=load_identity().peer_id,
            on_connection=self._handle_connection,
            port=settings.sendspin_port,
            client_name=settings.name,
        )
        await self.listener.start()
        log.info("Sendspin source client advertising on port %d", settings.sendspin_port)

    async def _handle_connection(self, websocket: Any) -> None:
        assert self.pairing_store is not None
        client = SendspinClient(
            identity=load_identity(),
            client_name=settings.name,
            roles=[Roles.SOURCE],
            pairing_store=self.pairing_store,
            pairing_support=PairingSupport(
                pin_display=self._show_pairing_code,
                offer_static_pin=bool(settings.sendspin_pairing_code),
            ),
            source_support=ClientHelloSourceSupport(
                features=ClientHelloSourceFeatures(line_sense=False)
            ),
        )
        client.add_server_command_listener(self._server_command)
        self.client = client
        await client.attach_websocket(websocket)
        if self.client is client:
            await self._stop_capture()
            self.client = None

    async def _show_pairing_code(self, code: str | None) -> None:
        global pairing_code
        pairing_code = code or ""
        if pairing_code:
            log.info("Sendspin pairing code: %s", pairing_code)
        else:
            log.info("Sendspin pairing ended")

    def _server_command(self, payload: Any) -> None:
        source = getattr(payload, "source", None)
        if source is None:
            return
        asyncio.create_task(self._handle_source_command(source.command))

    async def _handle_source_command(self, command: str) -> None:
        if command == "start":
            await self._start_capture()
        elif command == "stop":
            await self._stop_capture()

    async def _start_capture(self) -> None:
        if self.capture_task and not self.capture_task.done():
            return
        if not self.client or not self.client.connected:
            return
        self.source_capture = self.client.create_source_capture(
            SupportedAudioFormat(
                codec=AudioCodec.PCM,
                sample_rate=settings.sample_rate,
                bit_depth=16,
                channels=settings.channels,
            )
        )
        await self.source_capture.start()
        self.capture_task = asyncio.create_task(self._feed_capture())
        log.info("Music Assistant requested turntable audio")

    async def _feed_capture(self) -> None:
        assert self.source_capture is not None
        frame_bytes = settings.channels * 2
        chunk_bytes = settings.sample_rate * frame_bytes * settings.capture_chunk_ms // 1000
        prebuffer_chunks = max(1, settings.capture_buffer_ms // settings.capture_chunk_ms)
        pending = bytearray()
        buffered: list[bytes] = []
        try:
            async for data in capture.stream():
                pending.extend(data)
                while len(pending) >= chunk_bytes:
                    chunk = bytes(pending[:chunk_bytes])
                    del pending[:chunk_bytes]
                    if len(buffered) < prebuffer_chunks:
                        buffered.append(chunk)
                        if len(buffered) < prebuffer_chunks:
                            continue
                    else:
                        buffered.append(chunk)
                    next_chunk = buffered.pop(0)
                    duration_us = len(next_chunk) * 1_000_000 // (settings.sample_rate * frame_bytes)
                    capture_timestamp_us = time.monotonic_ns() // 1000 - duration_us
                    await self.source_capture.feed(next_chunk, capture_timestamp_us=capture_timestamp_us)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Sendspin source capture failed")

    async def _stop_capture(self) -> None:
        if self.capture_task and not self.capture_task.done():
            self.capture_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.capture_task
        self.capture_task = None
        if self.source_capture is not None:
            with contextlib.suppress(Exception):
                await self.source_capture.stop()
        self.source_capture = None
        await capture.stop()

    async def stop(self) -> None:
        await self._stop_capture()
        if self.client:
            with contextlib.suppress(Exception):
                await self.client.disconnect()
        if self.listener:
            await self.listener.stop()


async def music_assistant() -> dict[str, Any]:
    if not settings.ma_url or not settings.ma_entity:
        return {"configured": False, "title": "Music Assistant not configured"}
    headers = {"Authorization": f"Bearer {settings.ma_token}"} if settings.ma_token else {}
    url = f"{settings.ma_url}/api/states/{settings.ma_entity}"
    try:
        async with httpx.AsyncClient(timeout=4) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
        attributes = data.get("attributes", {})
        title = attributes.get("media_title") or attributes.get("title") or "Nothing playing"
        artist = attributes.get("media_artist") or attributes.get("artist") or ""
        album = attributes.get("media_album_name") or attributes.get("album_name") or ""
        return {
            "configured": True,
            "state": data.get("state"),
            "title": title,
            "artist": artist,
            "album": album,
        }
    except Exception as exc:
        return {"configured": True, "title": "Music Assistant unavailable", "error": str(exc)}


def wav_header() -> bytes:
    byte_rate = settings.sample_rate * settings.channels * 2
    block_align = settings.channels * 2
    return (
        b"RIFF\xff\xff\xff\xffWAVEfmt "
        + (16).to_bytes(4, "little")
        + (1).to_bytes(2, "little")
        + settings.channels.to_bytes(2, "little")
        + settings.sample_rate.to_bytes(4, "little")
        + byte_rate.to_bytes(4, "little")
        + block_align.to_bytes(2, "little")
        + (16).to_bytes(2, "little")
        + b"data\xff\xff\xff\xff"
    )


app = FastAPI(title=f"{settings.name} Control")
source_bridge = SendspinSourceBridge()


@app.get("/audio.wav")
async def audio_stream() -> StreamingResponse:
    return StreamingResponse(capture_stream(), media_type="audio/wav")


async def capture_stream() -> AsyncIterator[bytes]:
    yield wav_header()
    async for data in capture.stream():
        yield data


@app.get("/api/status")
async def api_status() -> JSONResponse:
    return JSONResponse(
        {
            "capture": capture.status(),
            "now_playing": now_playing,
            "pairing_code": pairing_code or settings.sendspin_pairing_code,
            "music_assistant": await music_assistant(),
            "sendspin": {
                "port": settings.sendspin_port,
                "running": source_bridge.connected,
            },
        }
    )


@app.post("/api/metadata")
async def set_metadata(payload: dict[str, Any]) -> JSONResponse:
    now_playing.update(
        {
            "title": str(payload.get("title", "")).strip(),
            "artist": str(payload.get("artist", "")).strip(),
            "album": str(payload.get("album", "")).strip(),
            "source": "manual",
        }
    )
    log.info("Manual metadata set: %s - %s", now_playing["artist"], now_playing["title"])
    return JSONResponse({"ok": True, "now_playing": now_playing})


@app.delete("/api/metadata")
async def clear_metadata() -> JSONResponse:
    now_playing.update({"title": "", "artist": "", "album": "", "source": "manual"})
    log.info("Now-playing metadata cleared")
    return JSONResponse({"ok": True, "now_playing": now_playing})


async def recognize_latest() -> dict[str, Any]:
    global last_recognition_key
    if not settings.audd_token:
        return {"ok": False, "error": "AUDD_TOKEN is not configured."}
    audio = capture.recent_wav()
    if len(audio) < settings.sample_rate * settings.channels * 2 * 5:
        return {"ok": False, "error": "Not enough captured audio yet."}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                "https://api.audd.io/",
                data={"api_token": settings.audd_token, "return": "apple_music,spotify"},
                files={"file": ("turntable.wav", audio, "audio/wav")},
            )
            response.raise_for_status()
            result = response.json().get("result") or {}
        if not result:
            return {"ok": False, "error": "No matching song was found."}
        match_key = f"{result.get('artist', '')}|{result.get('title', '')}|{result.get('album', '')}"
        if match_key == last_recognition_key:
            return {"ok": True, "duplicate": True, "now_playing": now_playing}
        last_recognition_key = match_key
        now_playing.update(
            {
                "title": str(result.get("title", "")),
                "artist": str(result.get("artist", "")),
                "album": str(result.get("album", "")),
                "source": "AudD recognition",
            }
        )
        log.info("Recognized metadata: %s - %s", now_playing["artist"], now_playing["title"])
        return {"ok": True, "now_playing": now_playing}
    except Exception as exc:
        log.exception("Audio recognition failed")
        return {"ok": False, "error": str(exc)}


@app.post("/api/recognize")
async def recognize_audio() -> JSONResponse:
    result = await recognize_latest()
    status = 200 if result.get("ok") else 502
    return JSONResponse(result, status_code=status)


async def auto_recognize_loop() -> None:
    while True:
        await asyncio.sleep(max(10, settings.recognition_interval))
        if not settings.auto_recognize or not settings.audd_token or not capture.running:
            continue
        result = await recognize_latest()
        if result.get("ok") and not result.get("duplicate"):
            log.info("Automatic recognition updated the current record")


@app.get("/api/logs")
async def api_logs() -> JSONResponse:
    return JSONResponse({"logs": list(logs)})


@app.post("/api/test-pi")
async def test_pi() -> JSONResponse:
    return JSONResponse(await capture.test())


@app.post("/api/pairing/open")
async def open_pairing() -> JSONResponse:
    if source_bridge.client:
        source_bridge.client.open_pairing_window()
    return JSONResponse(
        {
            "ok": True,
            "pairing_code": pairing_code or settings.sendspin_pairing_code,
            "message": "Pairing is ready. Open Music Assistant and add the discovered Turntable source.",
        }
    )


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    title = html.escape(settings.name)
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} | Turntable</title><script src="https://unpkg.com/lucide@latest"></script>
<style>
:root{{--ink:#f8f5ef;--muted:#a9a59d;--panel:#17191b;--panel2:#202326;--line:#303438;--accent:#e9b44c;--green:#75d69a;--red:#ef7770}}
*{{box-sizing:border-box}}body{{margin:0;background:#0e1011;color:var(--ink);font:15px ui-sans-serif,system-ui,sans-serif;background-image:radial-gradient(circle at 80% -20%,#4a3520 0,transparent 34%),linear-gradient(135deg,#0e1011,#151719 60%,#11100e)}}
.shell{{max-width:1180px;margin:auto;padding:38px 24px 56px}}.top{{display:flex;align-items:center;justify-content:space-between;margin-bottom:34px}}.brand{{display:flex;gap:14px;align-items:center}}.record{{width:46px;height:46px;border-radius:50%;background:radial-gradient(circle,#e9b44c 0 8%,#17191b 9% 16%,#414448 17% 18%,#111 19% 100%);box-shadow:0 0 0 7px #202326}}h1{{font:600 29px Georgia,serif;margin:0}}.eyebrow{{color:var(--accent);font-size:11px;text-transform:uppercase;letter-spacing:.16em;margin-bottom:5px}}.pill{{border:1px solid var(--line);padding:8px 12px;border-radius:99px;color:var(--muted);font-size:12px}}.grid{{display:grid;grid-template-columns:1.2fr .8fr;gap:18px}}.panel{{background:linear-gradient(145deg,#1a1c1f,#141618);border:1px solid var(--line);border-radius:12px;padding:24px;box-shadow:0 14px 40px #0004}}.hero{{min-height:290px;display:flex;flex-direction:column;justify-content:space-between}}.hero h2{{font:500 42px Georgia,serif;max-width:580px;margin:20px 0 10px;line-height:1.05}}.hero p{{color:var(--muted);max-width:580px;line-height:1.6}}.status{{display:flex;align-items:center;gap:9px;color:var(--green);font-size:13px}}.dot{{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 13px var(--green)}}.section-title{{display:flex;align-items:center;gap:10px;font-weight:600;margin:0 0 18px}}.section-title i{{color:var(--accent)}}.metric{{display:flex;justify-content:space-between;padding:13px 0;border-bottom:1px solid var(--line);color:var(--muted)}}.metric strong{{color:var(--ink);font-weight:500;text-align:right;max-width:60%;overflow:hidden;text-overflow:ellipsis}}button{{border:0;background:var(--accent);color:#17130b;padding:11px 16px;border-radius:7px;font-weight:700;cursor:pointer;display:inline-flex;gap:8px;align-items:center}}button.secondary{{background:#2a2d30;color:var(--ink);border:1px solid #3a3e42}}.actions{{display:flex;gap:10px;flex-wrap:wrap}}.now{{font:500 25px Georgia,serif;margin:4px 0 7px}}.sub{{color:var(--muted);min-height:22px}}pre{{white-space:pre-wrap;max-height:260px;overflow:auto;color:#b9c0bd;background:#0c0e0f;padding:15px;border-radius:7px;font-size:12px;line-height:1.55}}.wide{{grid-column:1/-1}}.note{{color:var(--muted);font-size:13px;line-height:1.5}}@media(max-width:800px){{.grid{{grid-template-columns:1fr}}.wide{{grid-column:auto}}.hero h2{{font-size:35px}}.top{{align-items:flex-start;gap:18px;flex-direction:column}}}}
 </style></head><body><main class="shell"><header class="top"><div class="brand"><div class="record"></div><div><div class="eyebrow">Live audio source</div><h1>{title}</h1></div></div><div class="pill"><i data-lucide="radio" width="14"></i> Source client :{settings.sendspin_port}</div></header>
<section class="grid"><article class="panel hero"><div><div class="status"><span class="dot"></span> Control bridge online</div><h2>Your records, everywhere.</h2><p>USB audio enters through the Raspberry Pi, crosses the network securely, and arrives in Music Assistant as a synchronized source.</p></div><div class="actions"><button onclick="testPi()"><i data-lucide="scan-line" width="16"></i> Test Raspberry Pi</button><button onclick="openPairing()" class="secondary"><i data-lucide="key-round" width="16"></i> Open pairing</button><button class="secondary" onclick="refresh()"><i data-lucide="refresh-cw" width="16"></i> Refresh</button></div></article>
<article class="panel"><h3 class="section-title"><i data-lucide="disc-3" width="18"></i> Signal path</h3><div class="metric"><span>Raspberry Pi</span><strong id="pi">checking...</strong></div><div class="metric"><span>USB capture</span><strong id="device">checking...</strong></div><div class="metric"><span>Sendspin</span><strong id="sendspin">checking...</strong></div><div class="metric"><span>Audio</span><strong>{settings.sample_rate // 1000} kHz / {settings.channels} ch</strong></div></article>
<article class="panel"><h3 class="section-title"><i data-lucide="music-2" width="18"></i> Now playing</h3><div class="now" id="song">No record selected</div><div class="sub" id="artist"></div><div style="display:grid;gap:8px;margin-top:18px"><input id="artist-input" placeholder="Artist" style="padding:10px;border-radius:6px;border:1px solid var(--line);background:#0c0e0f;color:var(--ink)"><input id="album-input" placeholder="Album" style="padding:10px;border-radius:6px;border:1px solid var(--line);background:#0c0e0f;color:var(--ink)"><input id="title-input" placeholder="Track" style="padding:10px;border-radius:6px;border:1px solid var(--line);background:#0c0e0f;color:var(--ink)"></div><div class="actions" style="margin-top:12px"><button onclick="setMetadata()"><i data-lucide="check" width="16"></i> Set metadata</button><button class="secondary" onclick="recognize()"><i data-lucide="scan-search" width="16"></i> Recognize</button></div><p class="note" id="metadata-note">Manual metadata stays local to this bridge. Recognition uses AudD when AUDD_TOKEN is configured.</p></article>
<article class="panel"><h3 class="section-title"><i data-lucide="wrench" width="18"></i> Diagnostics</h3><p class="note">The bridge uses the SSH key mounted from <code>/mnt/user/appdata/turntable/ssh</code>. Pi checks and capture errors appear in the log below.</p><pre id="logs">Loading logs...</pre></article>
<article class="panel wide"><h3 class="section-title"><i data-lucide="activity" width="18"></i> System status</h3><div id="result" class="note">Ready.</div></article></section></main><script>
lucide.createIcons();async function refresh(){{try{{const r=await fetch('/api/status');const d=await r.json();document.getElementById('pi').textContent=d.capture.pi;document.getElementById('device').textContent=d.capture.running?'Streaming':'Waiting';document.getElementById('sendspin').textContent=d.sendspin.running?'Connected':'Waiting for Music Assistant';const code=d.pairing_code||'Waiting...';document.getElementById('pairing-code').textContent=code;document.getElementById('pairing-message').textContent=d.pairing_code?'Enter this code in Music Assistant.':'Waiting for Music Assistant to begin pairing.';const n=d.now_playing;document.getElementById('song').textContent=n.title||'No record selected';document.getElementById('artist').textContent=[n.artist,n.album].filter(Boolean).join(' · ')||n.source;document.getElementById('artist-input').value=n.artist;document.getElementById('album-input').value=n.album;document.getElementById('title-input').value=n.title;const l=await (await fetch('/api/logs')).json();document.getElementById('logs').textContent=l.logs.join('\\n')||'No logs yet.';}}catch(e){{document.getElementById('result').textContent='Dashboard API unavailable: '+e}}}}async function openPairing(){{document.getElementById('pairing-modal').style.display='flex';const r=await fetch('/api/pairing/open',{{method:'POST'}});const d=await r.json();document.getElementById('pairing-message').textContent=d.message||d.error||'Pairing is ready.';if(d.pairing_code)document.getElementById('pairing-code').textContent=d.pairing_code;refresh()}}function closePairing(){{document.getElementById('pairing-modal').style.display='none'}}async function setMetadata(){{const payload={{artist:document.getElementById('artist-input').value,album:document.getElementById('album-input').value,title:document.getElementById('title-input').value}};const r=await fetch('/api/metadata',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(payload)}});const d=await r.json();document.getElementById('metadata-note').textContent=d.ok?'Metadata saved for this record.':d.error||'Could not save metadata.';refresh()}}async function recognize(){{document.getElementById('metadata-note').textContent='Listening for a match...';const r=await fetch('/api/recognize',{{method:'POST'}});const d=await r.json();document.getElementById('metadata-note').textContent=d.ok?'Recognition result saved.':d.error||'Recognition failed.';refresh()}}async function testPi(){{document.getElementById('result').textContent='Testing SSH and listing ALSA devices...';const r=await fetch('/api/test-pi',{{method:'POST'}});const d=await r.json();document.getElementById('result').textContent=(d.ok?'Pi connection OK\\n':'Pi test failed\\n')+d.output;refresh()}}refresh();setInterval(refresh,5000);
</script><div id="pairing-modal" style="display:none;position:fixed;inset:0;background:#000b;z-index:10;align-items:center;justify-content:center;padding:24px"><div style="width:min(460px,100%);background:#1a1c1f;border:1px solid var(--accent);border-radius:14px;padding:30px;text-align:center;box-shadow:0 20px 80px #000"><div class="eyebrow">Sendspin source pairing</div><h2 style="font:500 34px Georgia,serif;margin:12px 0">Connect to Music Assistant</h2><p class="note">Open Music Assistant's Sendspin Source provider and select <strong style="color:var(--ink)">Turntable</strong>.</p><div id="pairing-code" style="font:700 44px ui-monospace,monospace;letter-spacing:.12em;color:var(--accent);margin:25px 0">Waiting...</div><p class="note" id="pairing-message">Waiting for the pairing code.</p><button class="secondary" onclick="closePairing()"><i data-lucide="x" width="16"></i> Close</button></div></div></body></html>'''


async def main() -> None:
    global recognition_task
    await source_bridge.start()
    recognition_task = asyncio.create_task(auto_recognize_loop())
    config = uvicorn.Config(app, host="0.0.0.0", port=settings.web_port, log_level="warning")
    server = uvicorn.Server(config)
    try:
        await server.serve()
    finally:
        if recognition_task and not recognition_task.done():
            recognition_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await recognition_task
        await source_bridge.stop()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
