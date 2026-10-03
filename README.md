# Turntable Sendspin Bridge

A small Unraid control plane for a USB turntable preamp connected to a Raspberry Pi Zero W. The service captures ALSA audio on the Pi over SSH, serves it through the official Sendspin server, and provides a dashboard for diagnostics and Music Assistant status.

## Unraid deployment

The GitHub Actions workflow publishes the image as `ghcr.io/jordancollins1/turntable-sendspin:latest` whenever `master` changes. In Unraid, use **Docker -> Add Container** and enter that image name, then add the ports, SSH volume, and environment variables below. For a local build instead, use `docker compose build`.

1. Add ports `8383:8383` and `8927:8927`.
2. Copy `.env.example` to `.env` and set the Pi and Music Assistant values.
3. Mount `/mnt/user/appdata/turntable/ssh` into `/config/ssh` read-only. Place the SSH private key at `id_rsa`.
4. Start the container.
5. Open `http://UNRAID_IP:8383` and use **Test Raspberry Pi**.
6. Open the Sendspin pairing portal from the dashboard or at `http://UNRAID_IP:8927`.

Do not commit `.env`, SSH keys, or Music Assistant tokens. See `TURNtable_UNRAID.md` for the full setup and troubleshooting guide.

## Track metadata

The dashboard supports manual artist, album, and track entry. This metadata is kept by the bridge for the current session and is the reliable option for vinyl.

Recognition is optional. Set `AUDD_TOKEN` in `.env` to enable the **Recognize** button. It sends a short rolling WAV sample to AudD, then fills the metadata fields when a match is found. Shazam does not provide a supported server-side API suitable for this container, so AudD is used as the pluggable recognition provider.
