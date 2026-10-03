# Turntable Sendspin Bridge

A small Unraid control plane for a USB turntable preamp connected to a Raspberry Pi Zero W. The service captures ALSA audio on the Pi over SSH, serves it through the official Sendspin server, and provides a dashboard for diagnostics and Music Assistant status.

## Unraid deployment

1. Build the image with `docker compose build`.
2. Copy `.env.example` to `.env` and set the Pi and Music Assistant values.
3. Mount `/mnt/user/appdata/turntable/ssh` into `/config/ssh` read-only. Place the SSH private key at `id_rsa`.
4. Start it with `docker compose up -d`.
5. Open `http://UNRAID_IP:8383` and use **Test Raspberry Pi**.
6. Open the Sendspin pairing portal from the dashboard or at `http://UNRAID_IP:8927`.

Do not commit `.env`, SSH keys, or Music Assistant tokens. See `TURNtable_UNRAID.md` for the full setup and troubleshooting guide.
