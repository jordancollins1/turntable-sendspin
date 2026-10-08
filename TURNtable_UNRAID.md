# Turntable Sendspin Bridge

This container runs the control dashboard on Unraid. The USB audio device stays attached to the Raspberry Pi Zero W. Audio is captured on the Pi with ALSA over SSH and published to Music Assistant as a Sendspin source client.

## Build

Build this folder as the Docker context because it contains the Dockerfile, requirements, and Python service (`turntable_source.py`). `requirements.txt` must include `shazamio` for song recognition:

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
/mnt/user/appdata/turntable/sendspin:/config/sendspin
```

The mounted folder should contain the private key used by the old SSH setup, normally `id_rsa`. Add `known_hosts` there if you want strict host-key verification; otherwise the bridge uses `accept-new` for the first connection.

The `sendspin` folder must be read-write. It stores the persistent Sendspin identity and pairing records so the source does not need to be paired again after an update or restart.

Recommended environment variables:

```text
TURNTABLE_NAME=Turntable
PI_HOST=raspberrypi.local
PI_USER=pi
PI_AUDIO_DEVICE=plughw:CARD=Device,DEV=0   # use the card name from `arecord -l`, e.g. CARD=CODEC
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
AUTO_RECOGNIZE=true
RECOGNITION_INTERVAL=45
CAPTURE_CHUNK_MS=100
CAPTURE_BUFFER_MS=400
ARECORD_BUFFER_US=500000
ARECORD_PERIOD_US=100000
SENDSPIN_PAIRING_CODE=
```

The token is only read from the container environment and is never shown in the dashboard. If Music Assistant is running standalone rather than through Home Assistant, leave these fields empty until its supported status API is selected.

The dashboard recognizes audio automatically while Music Assistant is actively listening. Recognition uses the free ShazamIO library and needs no API token: a rolling ~12 second sample is checked every `RECOGNITION_INTERVAL` seconds. Set `AUTO_RECOGNIZE=false` to disable it. Manual metadata and the **Recognize** button remain available. ShazamIO is unofficial and may break if Shazam changes its API.

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

Run `arecord -l` over SSH if the device name differs. For example, `card 1: CODEC [USB AUDIO  CODEC]` means `PI_AUDIO_DEVICE=plughw:CARD=CODEC,DEV=0`. Set `PI_AUDIO_DEVICE` to the exact stable ALSA name.

## Notes

The bridge advertises itself as a Sendspin source over mDNS. Keep both ports on the LAN and do not expose them directly to the internet.

Because discovery uses mDNS, the container must be on the host network or a network with its own LAN IP (Unraid `br0`) for Music Assistant to find it. In the default bridge mode the advertised address is not reachable from the LAN.

## Home Assistant now-playing sensor

When `MA_URL` and `MA_TOKEN` point at Home Assistant (URL plus a long-lived access token), the bridge publishes the current track to `sensor.turntable_now_playing` (change it with `HA_SENSOR`, or set `HA_PUBLISH=false` to turn it off). The sensor's state is the track title, with `artist`, `album` and `source` as attributes. Recognized tracks also set `entity_picture` to the album cover URL from Shazam, so Home Assistant entity and media cards show the art; manually entered tracks have no art. It updates whenever metadata is recognized, set manually or cleared, and is re-sent every recognition interval so it reappears after a Home Assistant restart. The sensor is created through Home Assistant's REST API, so it has no unique ID and can't be edited from the UI.

## Troubleshooting

- **Test Raspberry Pi** shows the raw SSH and `arecord -l` output, including host-key and permission errors.
- **"Waiting for Music Assistant":** check the network mode above, reload the Sendspin Source provider in Music Assistant, restart the container, and click **Open pairing**.
- **Capture drops on the Pi Zero W:** keepalives detect a dead SSH link within about 15 seconds; turning off wifi power saving (`sudo iw dev wlan0 set power_save off`) usually helps.
