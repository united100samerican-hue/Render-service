#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/.env"
COMPOSE_FILE="$ROOT/docker-compose.yml"

fail() { echo "ERROR: $*" >&2; exit 1; }
warn() { echo "WARNING: $*" >&2; }

[[ "$(uname -m)" == "aarch64" ]] || fail "This deployment must run natively on Oracle Ampere ARM64 (aarch64). Detected: $(uname -m)"
command -v docker >/dev/null 2>&1 || fail "Docker is not installed."
docker compose version >/dev/null 2>&1 || fail "Docker Compose plugin is not available."
[[ -f "$ENV_FILE" ]] || fail "Missing $ENV_FILE; copy .env.example to .env and fill production values."
[[ -f "$COMPOSE_FILE" ]] || fail "Missing $COMPOSE_FILE"

chmod 600 "$ENV_FILE"

# Keep the validation compatible with Docker Compose's env-file syntax instead of sourcing
# the file as a shell script. This avoids interpreting cookie/session contents as shell code.
python3 - "$ENV_FILE" <<'PYENV'
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
required = [
    "API_ID", "API_HASH", "BOT_TOKEN", "KEEPALIVE_SECRET", "YOUTUBE_COOKIES",
    "R2_ENDPOINT", "R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
]

def has_nonempty_key(name: str) -> bool:
    pat = re.compile(rf"^{re.escape(name)}\s*[=:]\s*(.*)$")
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = pat.match(line)
        if not m:
            continue
        value = m.group(1).strip()
        if value.startswith("'"):
            if value == "''":
                return False
            if value == "'":
                j = i + 1
                chunks = []
                while j < len(lines):
                    if lines[j].endswith("'"):
                        chunks.append(lines[j][:-1])
                        break
                    chunks.append(lines[j])
                    j += 1
                return bool("\\n".join(chunks).strip())
        value = value.strip('"')
        return bool(value)
    return False

for name in required:
    if not has_nonempty_key(name):
        print(f"ERROR: {name} is missing or empty in {path}", file=sys.stderr)
        raise SystemExit(1)

if not has_nonempty_key("AUDIO_SESSION_STRING") and not has_nonempty_key("SESSION_STRING"):
    print("ERROR: AUDIO_SESSION_STRING and SESSION_STRING are both missing/empty.", file=sys.stderr)
    raise SystemExit(1)

for forbidden in ("SOCIAL_COOKIES_FILE", "SOCIAL_MEDIA_DIR", "AUDIO_API_URL", "TIKTOK_RTMP_URL", "TIKTOK_VIDEO_SIZE"):
    if re.search(rf"^{re.escape(forbidden)}\s*[=:]", text, flags=re.MULTILINE):
        print(f"ERROR: {forbidden} belongs to another service and must not be in the audio env file.", file=sys.stderr)
        raise SystemExit(1)
PYENV

# Validate Compose without printing the environment values.
docker compose -f "$COMPOSE_FILE" config --quiet

# Ensure only loopback is publishing the application port.
if command -v ss >/dev/null 2>&1; then
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    if [[ "$line" != *"127.0.0.1:10000"* && "$line" != *"[::1]:10000"* ]]; then
      fail "Host port 10000 is not loopback-only: $line"
    fi
  done < <(ss -lntH 'sport = :10000' 2>/dev/null || true)
fi

echo "Preflight OK: native ARM64, Docker/Compose available, env file protected, Compose valid."
