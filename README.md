# Turntable Sendspin Bridge

A small Unraid control plane for a USB turntable preamp connected to a Raspberry Pi Zero W. The service captures ALSA audio on the Pi over SSH, publishes it as a Sendspin source client, and provides a dashboard for diagnostics and Music Assistant status.

## Unraid deployment

The GitHub Actions workflow publishes the image as `ghcr.io/jordancollins1/turntable-sendspin:latest` whenever `master` changes. In Unraid, use **Docker -> Add Container** and enter that image name, then add the ports, SSH volume, and environment variables below. For a local build instead, use `docker compose build`.

1. Add ports `8383:8383` and `8928:8928`.
2. Copy `.env.example` to `.env` and set the Pi and Music Assistant values.
3. Mount `/mnt/user/appdata/turntable/ssh` into `/config/ssh` read-only. Place the SSH private key at `id_rsa`.
4. Start the container.
5. Open `http://UNRAID_IP:8383` and use **Test Raspberry Pi**.
6. Click **Open pairing**, then pair the discovered source from Music Assistant's **Sendspin Source** provider.

Do not commit `.env`, SSH keys, or Music Assistant tokens. See `TURNtable_UNRAID.md` for the full setup and troubleshooting guide.

## Track metadata

The dashboard supports manual artist, album, and track entry. This metadata is kept by the bridge for the current session and is the reliable option for vinyl.

The bridge is a Sendspin `source@v1` client, not a Sendspin Party server. After pairing, the turntable appears under Music Assistant **Live Inputs**.

Recognition is automatic when `AUDD_TOKEN` is set. The bridge checks the rolling audio buffer every 30 seconds by default and fills the metadata fields when a match is found. Change `RECOGNITION_INTERVAL` or set `AUTO_RECOGNIZE=false` in `.env` to adjust this. The **Recognize** button remains available for an immediate retry. Shazam does not provide a supported server-side API suitable for this container, so AudD is used as the pluggable recognition provider.
