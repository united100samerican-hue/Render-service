#!/bin/sh
set -eu

POT_HOME="${POT_PROVIDER_HOME:-/opt/bgutil-ytdlp-pot-provider}"
POT_PORT="${POT_PROVIDER_PORT:-4416}"
POT_HOST="127.0.0.1"
POT_PID=""

cleanup() {
    if [ -n "$POT_PID" ] && kill -0 "$POT_PID" 2>/dev/null; then
        kill "$POT_PID" 2>/dev/null || true
        wait "$POT_PID" 2>/dev/null || true
    fi
}

trap cleanup INT TERM EXIT

if [ -f "$POT_HOME/src/main.ts" ] && [ -x /usr/local/bin/deno ]; then
    /usr/local/bin/deno run \
        --no-prompt \
        --allow-env \
        --allow-net \
        --allow-ffi="$POT_HOME/node_modules" \
        --allow-read="$POT_HOME/node_modules" \
        "$POT_HOME/src/main.ts" \
        --host "$POT_HOST" \
        --port "$POT_PORT" \
        > /tmp/bgutil-pot-provider.log 2>&1 &
    POT_PID=$!

    READY=0
    i=0
    while [ "$i" -lt 120 ]; do
        if curl -fsS --max-time 1 -o /dev/null \
            "http://${POT_HOST}:${POT_PORT}/ping" 2>/dev/null; then
            READY=1
            break
        fi

        if ! kill -0 "$POT_PID" 2>/dev/null; then
            echo "WARNING: BgUtils provider exited during startup; YouTube POT support is unavailable." >&2
            cat /tmp/bgutil-pot-provider.log >&2 || true
            POT_PID=""
            break
        fi

        i=$((i + 1))
        sleep 0.25
    done

    if [ "$READY" -eq 1 ]; then
        echo "BgUtils POT provider ready on ${POT_HOST}:${POT_PORT}"
    elif [ -n "$POT_PID" ]; then
        echo "WARNING: BgUtils provider did not become ready within 30 seconds; continuing with audio service." >&2
        cat /tmp/bgutil-pot-provider.log >&2 || true
    fi
else
    echo "WARNING: BgUtils provider files/runtime are unavailable; continuing with audio service." >&2
fi

exec uvicorn app:app --host 0.0.0.0 --port "${PORT:-10000}" --workers 1
