#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT/docker-compose.yml"
SERVICE="audio"
CONTAINER="render-audio"
log() { logger -t render-audio-monitor -- "$*"; echo "$(date -Is) $*"; }

# 1) Container lifecycle: Docker's unless-stopped policy handles ordinary process exits.
if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
  log "container missing; starting audio service"
  docker compose -f "$COMPOSE_FILE" up -d "$SERVICE" || log "ERROR: failed to start missing container"
elif [[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null || echo false)" != "true" ]]; then
  log "container stopped; starting audio service"
  docker compose -f "$COMPOSE_FILE" up -d "$SERVICE" || log "ERROR: failed to start stopped container"
fi

# 2) Local liveness. Do not use /health as the restart criterion because ready=false can reflect
#    a real configuration/Telegram auth problem that should be investigated rather than looped.
if ! curl -fsS --max-time 5 http://127.0.0.1:10000/ping >/dev/null 2>&1; then
  log "WARNING: local /ping failed; restarting audio container"
  docker compose -f "$COMPOSE_FILE" restart "$SERVICE" || log "ERROR: restart failed"
fi

# 3) Resource warnings.
if df -P /srv/audio-media >/dev/null 2>&1; then
  disk_used="$(df -P /srv/audio-media | awk 'NR==2 {gsub(/%/,"",$5); print $5}')"
  if [[ "${disk_used:-0}" -ge 85 ]]; then
    log "WARNING: /srv/audio-media disk usage is ${disk_used}%"
  fi
fi

if command -v free >/dev/null 2>&1; then
  mem_used="$(free | awk '/Mem:/ {printf "%d", ($3/$2)*100}')"
  if [[ "${mem_used:-0}" -ge 85 ]]; then
    log "WARNING: system memory usage is ${mem_used}%"
  fi
fi

# 4) Tunnel status: report but do not restart blindly on an authentication/configuration failure.
if command -v systemctl >/dev/null 2>&1; then
  if ! systemctl is-active --quiet cloudflared 2>/dev/null; then
    log "WARNING: cloudflared systemd service is not active"
  fi
fi

# 5) Host exposure check: 10000 must remain loopback-only. BgUtils 4416 must never be published.
if command -v ss >/dev/null 2>&1; then
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    if [[ "$line" != *"127.0.0.1:10000"* && "$line" != *"[::1]:10000"* ]]; then
      log "ERROR: unexpected public bind on port 10000: $line"
    fi
  done < <(ss -lntH 'sport = :10000' 2>/dev/null || true)

  if ss -lntH 'sport = :4416' 2>/dev/null | grep -q .; then
    log "ERROR: host port 4416 is listening; BgUtils must remain container-local"
  fi
fi
