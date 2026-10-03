# Turntable Sendspin Bridge

This container runs the control dashboard on Unraid. The USB audio device stays attached to the Raspberry Pi Zero W. Audio is captured on the Pi with ALSA over SSH, exposed as a live WAV stream, and served to Music Assistant through Sendspin.

## Build

Build this folder as the Docker context because it contains the Dockerfile, requirements, and Python service:

```bash
docker build -t turntable-sendspin .
```

## Unraid container settings

Publish these ports:

- `8383:8383` - control dashboard
- `8927:8927` - Sendspin server and pairing portal

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
SENDSPIN_PORT=8927
```

For the Music Assistant card, configure the Home Assistant REST endpoint for the player that receives the turntable stream:

```text
MA_URL=http://homeassistant:8123
MA_TOKEN=<Home Assistant long-lived access token>
MA_ENTITY=media_player.turntable
```

The token is only read from the container environment and is never shown in the dashboard. If Music Assistant is running standalone rather than through Home Assistant, leave these fields empty until its supported status API is selected.

## First run

1. Open `http://UNRAID_HOST:8383`.
2. Select **Test Raspberry Pi** and confirm the USB capture device appears in the output.
3. Open **Open Sendspin** from the dashboard header on port `8927`.
4. Pair the Music Assistant Sendspin player there.
5. Start playback in Music Assistant. The dashboard will begin the SSH/ALSA capture when Sendspin requests audio.

On the Pi, the capture command used by the bridge is equivalent to:

```bash
arecord -D plughw:CARD=Device,DEV=0 -f S16_LE -r 48000 -c 2 -t raw
```

Run `arecord -l` over SSH if the device name differs. Set `PI_AUDIO_DEVICE` to the exact stable ALSA name.

## Notes

The dashboard port is intentionally separate from Sendspin's port. The latter is the pairing and player portal provided by the official Sendspin CLI. Keep both ports on the LAN and do not expose them directly to the internet.
