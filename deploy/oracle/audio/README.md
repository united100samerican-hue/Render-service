# Oracle deployment: services/audio only

This deployment layer runs the repository's existing `services/audio` service on an Oracle Cloud Infrastructure Ampere A1 ARM64 VM. It does not start `services/YouTube` or `services/tiktok`, and it does not modify the audio service source code.

## Source-of-truth findings from the supplied repository

- Build context is `services/audio` and the existing Dockerfile is used unchanged.
- The image is `python:3.12-slim` plus FFmpeg, Deno, BgUtils POT Provider `2.0.0`, yt-dlp, Telethon and PyTgCalls.
- BgUtils runs inside the same container on `127.0.0.1:4416`; no host port is published for it.
- Uvicorn listens on port `10000`.
- `services/audio/config.py` gives `AUDIO_SESSION_STRING` priority over `SESSION_STRING`.
- Temporary files remain under the existing `/tmp/render_audio_media` path; the Compose file bind-mounts `/srv/audio-media` there without changing Python code.
- R2 integration remains exactly where it is in the current code.
- YouTube cache keys remain `video720` / `v3`; no 480p change is made here.
- The current `/enqueue`, `/queue/list`, `/queue/clear` and `/queue/skip` endpoints are retained as-is. In this supplied version they do not implement a persistent queue; migration does not add queue logic.

## Oracle VM

Use one Oracle account. During sign-up, select the home region deliberately; Oracle documents that the home region is where Always Free compute and block storage are provisioned. Current Oracle Free Tier documentation states that an Always Free tenancy can use up to 2 OCPUs and 12 GB RAM in total on Ampere A1, and that the 200 GB Always Free block-volume allowance is shared by boot and data volumes. Oracle also states that `Out of host capacity` is a temporary capacity condition and recommends another availability domain in the same region or retrying later. See the official docs linked below.

Recommended instance for this project:

- Shape: `VM.Standard.A1.Flex`
- OCPU: `2`
- Memory: `12 GB`
- Image: Ubuntu ARM64 / aarch64, preferably an LTS release marked Always Free Eligible
- Public IPv4: enabled for initial SSH administration; application port remains private/loopback-only
- SSH: allow TCP 22 from your administration IP if you can restrict it; do not open 10000 or 4416

Frankfurt currently has 3 availability domains according to Oracle region documentation, while Milan has 1. Multiple availability domains can provide more placement choices when A1 capacity is constrained, but this is not a guarantee that A1 will be available. Do the capacity check in the Console at sign-up time rather than choosing a region solely from geography.

Oracle currently also documents an idle-instance reclamation policy for Always Free compute: an instance can be deemed idle over a 7-day period when CPU, network and (for A1) memory utilization are all below 20% at the stated threshold. Keep this in mind for a bot that may have long periods of inactivity. The Always Free allowance also includes 10 TB/month outbound data transfer.

## Storage

Create the host temporary-media directory:

```bash
sudo mkdir -p /srv/audio-media
sudo chmod 700 /srv/audio-media
```

The Compose bind mount is:

`/srv/audio-media` -> `/tmp/render_audio_media`

The application still calls `tempfile.gettempdir()` and therefore its Python behavior is unchanged. This directory is disposable working storage, not the authoritative media archive. R2 remains the durable cache.

## Clone the repository

```bash
sudo mkdir -p /opt
sudo chown "$USER":"$USER" /opt
cd /opt
git clone <YOUR-REPOSITORY-URL> Render-service
cd /opt/Render-service
```

If you are transferring the supplied archive instead of using Git, extract it so that the repository root is exactly:

`/opt/Render-service`

and therefore:

`/opt/Render-service/services/audio`

Do not move `services/audio` to another root.

## Install Docker and Compose

Use the official Docker APT repository method for your Ubuntu LTS release. Docker currently supports Ubuntu 22.04, 24.04 and 26.04 on ARM64, and the official package set includes the Compose plugin. Confirm that:

```bash
docker version
docker compose version
```

Both must work for the normal user that will operate the service. Enable Docker at boot:

```bash
sudo systemctl enable --now docker
```

## Configure production environment

Copy the template:

```bash
cd /opt/Render-service/deploy/oracle/audio
cp .env.example .env
chmod 600 .env
```

Populate `.env` with the CURRENT production values from Railway without changing variable names or the production values.

Required for the audio service:

`API_ID`
`API_HASH`
`AUDIO_SESSION_STRING`
`SESSION_STRING`
`BOT_TOKEN`
`KEEPALIVE_SECRET`
`YOUTUBE_COOKIES`
`YOUTUBE_PROXY`
`R2_ENDPOINT`
`R2_BUCKET`
`R2_ACCESS_KEY_ID`
`R2_SECRET_ACCESS_KEY`
`R2_PRESIGN_SECONDS`

`PORT` is forced to `10000` by the deployment file, matching the existing default.

Keep `AUDIO_SESSION_STRING` exactly as the production audio session. The current code uses it first; `SESSION_STRING` is only a fallback. Do not change or regenerate the session during the migration.

For a multiline `YOUTUBE_COOKIES` value, Docker Compose environment files support single-quoted multiline values. Preserve the current cookie content rather than replacing it with a different browser/session export. Do not put that value in Git.

Do not add `services/YouTube` variables such as `SOCIAL_COOKIES_FILE` or `AUDIO_API_URL`, and do not add TikTok variables in this first deployment.

## Build natively on Oracle ARM64

Before building:

```bash
uname -m
```

Expected:

`aarch64`

Then run the non-secret validation:

```bash
cd /opt/Render-service/deploy/oracle/audio
./scripts/preflight.sh
```

Build only the audio image:

```bash
docker compose build audio
```

This intentionally has no `platform: linux/amd64` override. Docker builds using the host architecture, so on this VM the result is ARM64.

Verify the image architecture:

```bash
IMAGE_ID="$(docker compose images -q audio)"
docker image inspect "$IMAGE_ID" --format '{{.Architecture}}'
```

Expected result: `arm64`.

## Start the service

For the first controlled startup:

```bash
docker compose up -d audio
docker compose ps
```

Then verify locally:

```bash
curl -fsS http://127.0.0.1:10000/ping
curl -fsS http://127.0.0.1:10000/health
```

The health response should report `ok: true`. `ready` should become `true` after Telegram initialization succeeds. YouTube status should report configured/loaded cookies and a reachable BgUtils provider with version `2.0.0`.

Do not run the same production `AUDIO_SESSION_STRING` on Railway and Oracle simultaneously.

## Boot-time automation

Install the provided Compose systemd unit:

```bash
sudo install -m 0644 systemd/render-audio-compose.service /etc/systemd/system/render-audio-compose.service
sudo systemctl daemon-reload
sudo systemctl enable --now render-audio-compose.service
```

The container itself also has `restart: unless-stopped`, so Docker handles ordinary container/process crashes. The systemd unit ensures the stack is brought up when the host boots.

Install the local health/resource monitor:

```bash
sudo install -m 0755 scripts/health-monitor.sh /opt/Render-service/deploy/oracle/audio/scripts/health-monitor.sh
sudo install -m 0644 systemd/render-audio-monitor.service /etc/systemd/system/render-audio-monitor.service
sudo install -m 0644 systemd/render-audio-monitor.timer /etc/systemd/system/render-audio-monitor.timer
sudo systemctl daemon-reload
sudo systemctl enable --now render-audio-monitor.timer
```

Inspect:

```bash
systemctl status render-audio-compose.service
systemctl status render-audio-monitor.timer
journalctl -u render-audio-monitor.service -n 50 --no-pager
```

The monitor restarts the audio container only when local liveness fails; it does not loop on `ready=false`, because a Telegram authorization or configuration problem should be investigated rather than repeatedly restarted.

## Cloudflare Tunnel

Use a permanent named/remote-managed Cloudflare Tunnel, not a Quick Tunnel.

In Cloudflare Zero Trust, create a tunnel and publish:

`audio.example.com` -> `http://127.0.0.1:10000`

Install `cloudflared` using the official Cloudflare Debian/Ubuntu repository. Then use the tunnel install command shown by the Cloudflare dashboard, typically:

```bash
sudo cloudflared service install <TUNNEL_TOKEN>
```

Then:

```bash
sudo systemctl enable --now cloudflared
sudo systemctl status cloudflared
```

Do not put `<TUNNEL_TOKEN>` in this repository. Do not use a Quick Tunnel for production because its hostname is ephemeral.

The Cloudflare route must point to the host loopback service. The application port remains unpublished externally; OCI should not have an ingress rule for TCP 10000 or TCP 4416.

If you prefer a locally-managed tunnel with a credentials JSON, the repository includes `cloudflared/config.example.yml` as a redacted template. Never commit the real credentials JSON.

## Production cut-over order

1. Keep Railway audio running while Oracle is built and tested without the production Telegram session.
2. Stop Railway audio and verify it is no longer using `AUDIO_SESSION_STRING`.
3. Start Oracle audio.
4. Wait for `/health` to report `ready=true`.
5. Verify `/youtube/status` using the `x-keepalive-secret` header.
6. Test Telegram audio playback.
7. Test Telegram video playback.
8. Test a YouTube URL.
9. Test the same YouTube URL again and verify the R2 cache path is being used from the application logs/behavior.
10. Test a supported live stream.
11. Change only the audio service base URL in the worker to the stable Cloudflare hostname.
12. Test the bot end-to-end.
13. Leave Railway intact but stopped as the rollback target until the migration has remained stable.

## Rollback

Never run both servers with the same production Telegram session. On a critical Oracle failure:

```text
stop Oracle audio
start Railway audio
restore Railway base URL in the worker
```

Only after Railway has resumed should Oracle remain stopped for investigation.

## Required verification after migration

Check:

```bash
curl -fsS http://127.0.0.1:10000/ping
curl -fsS http://127.0.0.1:10000/health
systemctl is-active docker
systemctl is-active render-audio-compose.service
systemctl is-active cloudflared
```

Also verify that host port 10000 is loopback-only and that host port 4416 is not listening:

```bash
sudo ss -lntp | grep -E ':(10000|4416)\b' || true
```

Expected application exposure on the host is loopback `127.0.0.1:10000`; there should be no host listener for 4416.

## Restart test

After all functional tests pass:

```bash
sudo reboot
```

After the VM returns, do not manually run `docker compose up`. Verify:

```bash
systemctl is-active docker
systemctl is-active render-audio-compose.service
systemctl is-active cloudflared
curl -fsS http://127.0.0.1:10000/health
```

Then test the Telegram bot.

## Crash-recovery test

```bash
docker stop render-audio
```

Within the next monitor/restart cycle, the container should return. Check:

```bash
docker ps --filter name=render-audio
curl -fsS http://127.0.0.1:10000/health
```

The Compose `unless-stopped` policy handles ordinary exits; the monitor also corrects a stopped/missing container and a failed local `/ping`.

## Logs and secrets

Compose caps Docker JSON logs at 10 MB per file with 5 retained files. Never commit or print `.env` contents. Never place any of these values in Git, image layers, URLs, or diagnostic output:

`AUDIO_SESSION_STRING`
`SESSION_STRING`
`API_HASH`
`BOT_TOKEN`
`KEEPALIVE_SECRET`
`YOUTUBE_COOKIES`
`R2_SECRET_ACCESS_KEY`

The existing application logs only metadata around cookie/provider state and does not intentionally log the cookie contents or environment secrets. The deployment layer does not echo secret values.

## What is deliberately NOT changed in this phase

- `services/audio/service.py`
- `services/audio/app.py`
- `services/audio/config.py`
- `services/audio/call/manager.py`
- `services/audio/media/url.py`
- `services/audio/media/cache.py`
- `services/audio/media/telegram.py`
- `services/audio/state/models.py`
- the existing audio HTTP routes
- `video720` cache naming
- the existing Telegram session model
- R2 cache behavior
- YouTube extraction logic
- video quality selection
- permanent object state / Durable Object ideas
- TikTok and recording/bridge work

Those are separate later phases after Oracle audio is proven stable.

## Official references

Oracle Free Tier: https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier.htm

Oracle Always Free resources: https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm

Oracle regions and availability domains: https://docs.oracle.com/en-us/iaas/Content/General/Concepts/regions.htm

Cloudflare cloudflared installation: https://developers.cloudflare.com/tunnel/downloads/

Cloudflare Tunnel as Linux service: https://developers.cloudflare.com/tunnel/features/locally-managed-tunnels/as-a-service/linux/

Cloudflare Tunnel routing: https://developers.cloudflare.com/tunnel/concepts/routing/

BgUtils POT Provider: https://github.com/Brainicism/bgutil-ytdlp-pot-provider

Docker Compose environment-file syntax: https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/
