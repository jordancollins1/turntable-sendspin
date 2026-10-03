#!/usr/bin/env python3
"""Turntable control plane for Unraid.

The Raspberry Pi owns the USB audio device. This service reads its raw ALSA
capture stream over SSH, exposes it as a live WAV stream, and lets the
official Sendspin CLI serve it to paired Music Assistant players.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import os
import shlex
import time
from collections import deque
from pathlib import Path
from typing import Any, AsyncIterator

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
    sendspin_port = int(os.getenv("SENDSPIN_PORT", "8927"))
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
                for subscriber in list(self.subscribers):
                    if not subscriber.full():
                        subscriber.put_nowait(data)
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
                stderr=asyncio.subprocess.PIPE,
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


capture = PiCapture()
sendspin_process: asyncio.subprocess.Process | None = None


async def start_sendspin() -> None:
    global sendspin_process
    if sendspin_process and sendspin_process.returncode is None:
        return
    command = [
        "sendspin",
        "serve",
        f"http://127.0.0.1:{settings.web_port}/audio.wav",
        "--source-format",
        "wav",
        "--name",
        settings.name,
        "--port",
        str(settings.sendspin_port),
    ]
    log.info("Starting Sendspin server on port %d", settings.sendspin_port)
    sendspin_process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )


async def read_sendspin_logs() -> None:
    if not sendspin_process or not sendspin_process.stdout:
        return
    while data := await sendspin_process.stdout.readline():
        log.info("Sendspin: %s", data.decode(errors="replace").strip())


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
            "music_assistant": await music_assistant(),
            "sendspin": {
                "port": settings.sendspin_port,
                "running": bool(sendspin_process and sendspin_process.returncode is None),
            },
        }
    )


@app.get("/api/logs")
async def api_logs() -> JSONResponse:
    return JSONResponse({"logs": list(logs)})


@app.post("/api/test-pi")
async def test_pi() -> JSONResponse:
    return JSONResponse(await capture.test())


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
</style></head><body><main class="shell"><header class="top"><div class="brand"><div class="record"></div><div><div class="eyebrow">Live audio source</div><h1>{title}</h1></div></div><div class="pill"><i data-lucide="wifi" width="14"></i> <a href="#" onclick="window.open('http://' + location.hostname + ':{settings.sendspin_port}')" style="color:inherit;text-decoration:none">Open Sendspin :{settings.sendspin_port}</a></div></header>
<section class="grid"><article class="panel hero"><div><div class="status"><span class="dot"></span> Control bridge online</div><h2>Your records, everywhere.</h2><p>USB audio enters through the Raspberry Pi, crosses the network securely, and arrives in Music Assistant as a synchronized source.</p></div><div class="actions"><button onclick="testPi()"><i data-lucide="scan-line" width="16"></i> Test Raspberry Pi</button><button class="secondary" onclick="refresh()"><i data-lucide="refresh-cw" width="16"></i> Refresh</button></div></article>
<article class="panel"><h3 class="section-title"><i data-lucide="disc-3" width="18"></i> Signal path</h3><div class="metric"><span>Raspberry Pi</span><strong id="pi">checking...</strong></div><div class="metric"><span>USB capture</span><strong id="device">checking...</strong></div><div class="metric"><span>Sendspin</span><strong id="sendspin">checking...</strong></div><div class="metric"><span>Audio</span><strong>{settings.sample_rate // 1000} kHz / {settings.channels} ch</strong></div></article>
<article class="panel"><h3 class="section-title"><i data-lucide="music-2" width="18"></i> Music Assistant</h3><div class="now" id="song">Loading now playing...</div><div class="sub" id="artist"></div><p class="note" id="ma-note">The current player state appears here when Music Assistant is configured.</p></article>
<article class="panel"><h3 class="section-title"><i data-lucide="wrench" width="18"></i> Diagnostics</h3><p class="note">The bridge uses the SSH key mounted from <code>/mnt/user/appdata/turntable/ssh</code>. Pi checks and capture errors appear in the log below.</p><pre id="logs">Loading logs...</pre></article>
<article class="panel wide"><h3 class="section-title"><i data-lucide="activity" width="18"></i> System status</h3><div id="result" class="note">Ready.</div></article></section></main><script>
lucide.createIcons();async function refresh(){{try{{const r=await fetch('/api/status');const d=await r.json();document.getElementById('pi').textContent=d.capture.pi;document.getElementById('device').textContent=d.capture.running?'Streaming':'Waiting';document.getElementById('sendspin').textContent=d.sendspin.running?'Listening':'Starting';const m=d.music_assistant;document.getElementById('song').textContent=m.title||'Nothing playing';document.getElementById('artist').textContent=[m.artist,m.album].filter(Boolean).join(' · ');document.getElementById('ma-note').textContent=m.error||(!m.configured?'Set MA_URL, MA_TOKEN and MA_ENTITY in the container template.':'Music Assistant status connected.');const l=await (await fetch('/api/logs')).json();document.getElementById('logs').textContent=l.logs.join('\\n')||'No logs yet.';}}catch(e){{document.getElementById('result').textContent='Dashboard API unavailable: '+e}}}}async function testPi(){{document.getElementById('result').textContent='Testing SSH and listing ALSA devices...';const r=await fetch('/api/test-pi',{{method:'POST'}});const d=await r.json();document.getElementById('result').textContent=(d.ok?'Pi connection OK\\n':'Pi test failed\\n')+d.output;refresh()}}refresh();setInterval(refresh,5000);
</script></body></html>'''


async def main() -> None:
    await start_sendspin()
    asyncio.create_task(read_sendspin_logs())
    config = uvicorn.Config(app, host="0.0.0.0", port=settings.web_port, log_level="warning")
    server = uvicorn.Server(config)
    try:
        await server.serve()
    finally:
        await capture.stop()
        if sendspin_process and sendspin_process.returncode is None:
            sendspin_process.terminate()
            await sendspin_process.wait()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
