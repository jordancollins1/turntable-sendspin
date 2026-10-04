# Turntable Sendspin Bridge

This container runs the control dashboard on Unraid. The USB audio device stays attached to the Raspberry Pi Zero W. Audio is captured on the Pi with ALSA over SSH and published to Music Assistant as a Sendspin source client.

## Build

Build this folder as the Docker context because it contains the Dockerfile, requirements, and Python service:

```bash
docker build -t turntable-sendspin .
```

## Unraid container settings

Publish these ports:

- `8383:8383` - control dashboard
- `8928:8928` - Sendspin source client connection

Add a read-only path mapping:

```text
/mnt/user/appdata/turntable/ssh:/config/ssh:ro
```

The mounted folder should contain the private key used by the old SSH setup, normally `id_rsa`. Add `known_hosts` there if you want strict host-key verification; otherwise the bridge uses `accept-new` for the first connection.

Recommended environment variables:

```text
TURNTABLE_NAME=Turntable
PI_HOST=raspberrypi.local
PI_USER=pi
PI_AUDIO_DEVICE=plughw:CARD=Device,DEV=0
AUDIO_RATE=48000
AUDIO_CHANNELS=2
SSH_KEY=/config/ssh/id_rsa
WEB_PORT=8383
SENDSPIN_PORT=8928
```

For the Music Assistant card, configure the Home Assistant REST endpoint for the player that receives the turntable stream:

```text
MA_URL=http://homeassistant:8123
MA_TOKEN=<Home Assistant long-lived access token>
MA_ENTITY=media_player.turntable
AUDD_TOKEN=
AUTO_RECOGNIZE=true
RECOGNITION_INTERVAL=30
CAPTURE_CHUNK_MS=100
CAPTURE_BUFFER_MS=400
SENDSPIN_PAIRING_CODE=
```

The token is only read from the container environment and is never shown in the dashboard. If Music Assistant is running standalone rather than through Home Assistant, leave these fields empty until its supported status API is selected.

The dashboard recognizes audio automatically while Music Assistant is actively listening. Add an AudD API token as `AUDD_TOKEN`; recognition checks a short rolling audio sample every `RECOGNITION_INTERVAL` seconds. Set `AUTO_RECOGNIZE=false` to disable it. Manual metadata and the **Recognize** button remain available. Shazam does not offer a supported server-side API for this Docker workflow.

## First run

1. Open `http://UNRAID_HOST:8383`.
2. Select **Test Raspberry Pi** and confirm the USB capture device appears in the output.
3. Click **Open pairing** in the dashboard.
4. Add/enable Music Assistant's **Sendspin Source** provider and pair the discovered `Turntable` source.
5. Start the source from Music Assistant under **Live Inputs**. The dashboard begins SSH/ALSA capture when Music Assistant requests audio.

On the Pi, the capture command used by the bridge is equivalent to:

```bash
arecord -D plughw:CARD=Device,DEV=0 -f S16_LE -r 48000 -c 2 -t raw
```

Run `arecord -l` over SSH if the device name differs. Set `PI_AUDIO_DEVICE` to the exact stable ALSA name.

## Notes

The bridge advertises itself as a Sendspin source over mDNS. Keep both ports on the LAN and do not expose them directly to the internet.
