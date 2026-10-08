# Turntable Sendspin Bridge

A small Unraid control plane for a USB turntable preamp connected to a Raspberry Pi Zero W. The service captures ALSA audio on the Pi over SSH, publishes it as a Sendspin source client, and provides a dashboard for diagnostics and Music Assistant status.

## Unraid deployment

The GitHub Actions workflow publishes the image as `ghcr.io/jordancollins1/turntable-sendspin:latest` whenever `master` changes. In Unraid, use **Docker -> Add Container** and enter that image name, then add the ports, SSH volume, and environment variables below. For a local build instead, use `docker compose build`.

1. Add ports `8383:8383` and `8928:8928`.
2. Copy `env.example` to `.env` and set the Pi and Music Assistant values.
3. Mount `/mnt/user/appdata/turntable/ssh` into `/config/ssh` read-only. Place the SSH private key at `id_rsa`.
4. Mount `/mnt/user/appdata/turntable/sendspin` into `/config/sendspin` read-write. This preserves the Sendspin identity and pairing records.
5. Start the container.
6. Open `http://UNRAID_IP:8383` and use **Test Raspberry Pi**.
7. Click **Open pairing**, then pair the discovered source from Music Assistant's **Sendspin Source** provider.

Do not commit `.env`, SSH keys, or Music Assistant tokens. See `TURNtable_UNRAID.md` for the full setup and troubleshooting guide.

## Track metadata

The dashboard supports manual artist, album, and track entry. This metadata is kept by the bridge for the current session and is the reliable option for vinyl.

The bridge is a Sendspin `source@v1` client, not a Sendspin Party server. After pairing, the turntable appears under Music Assistant **Live Inputs**.

The default capture settings use 100 ms packets and a 400 ms jitter buffer to smooth Raspberry Pi Wi-Fi/SSH timing. Increase `CAPTURE_BUFFER_MS` to 600 or 800 if the Pi network is still unstable; this adds latency but does not change pitch or quality.

The bridge also drains capture errors safely, applies a larger ALSA buffer on the Pi, and backpressures instead of dropping audio when the network falls behind.

Recognition is automatic and needs no API key. The bridge sends a rolling ~12 second audio sample to Shazam (through the free [ShazamIO](https://pypi.org/project/shazamio/) library) every 45 seconds by default and fills the metadata fields when a match is found. Change `RECOGNITION_INTERVAL` or set `AUTO_RECOGNIZE=false` in `.env` to adjust this. The **Recognize** button remains available for an immediate retry. ShazamIO is an unofficial, reverse-engineered client, so it can stop working if Shazam changes its API; manual metadata always works. If `shazamio` is not installed, the bridge still runs and recognition is simply disabled.

Recognized metadata is shown on the dashboard and available at `/api/status`. Music Assistant's Sendspin Source plugin does not currently take now-playing metadata from a source client.

## Home Assistant now-playing sensor

When `MA_URL` and `MA_TOKEN` point at Home Assistant (URL plus a long-lived access token), the bridge publishes the current track to `sensor.turntable_now_playing` (change it with `HA_SENSOR`, or set `HA_PUBLISH=false` to turn it off). The sensor's state is the track title, with `artist`, `album` and `source` as attributes. Recognized tracks also set `entity_picture` to the album cover URL from Shazam, so Home Assistant entity and media cards show the art; manually entered tracks have no art. It updates whenever metadata is recognized, set manually or cleared, and is re-sent every recognition interval so it reappears after a Home Assistant restart. The sensor is created through Home Assistant's REST API, so it has no unique ID and can't be edited from the UI.

## Troubleshooting

- **Test Raspberry Pi** shows the raw SSH/`arecord -l` output, including SSH errors such as host-key or permission failures.
- **"Waiting for Music Assistant" after a restart or network change:** the bridge only listens and advertises itself over mDNS, so Music Assistant must be able to discover and reach port 8928. Run the container on the host network or a custom network with its own LAN IP, then reload the Sendspin Source provider in Music Assistant and click **Open pairing**.
- **Capture keeps dropping:** SSH keepalives detect a dead Pi connection within about 15 seconds. On the Pi Zero W, disabling wifi power saving helps: `sudo iw dev wlan0 set power_save off`.
