#!/bin/sh
set -eu

VERSION="2.0.0"
DEST="/opt/bgutil-ytdlp-pot-provider"
TMP="/tmp/bgutil-ytdlp-pot-provider-${VERSION}.tar.gz"

export DENO_NO_UPDATE_CHECK=1
export DENO_NO_PROMPT=1

rm -rf "$DEST"
mkdir -p /opt

curl --retry 4 --retry-delay 2 --retry-all-errors -fsSL \
  "https://github.com/Brainicism/bgutil-ytdlp-pot-provider/archive/refs/tags/${VERSION}.tar.gz" \
  -o "$TMP"

tar -xzf "$TMP" -C /opt

mv \
  "/opt/bgutil-ytdlp-pot-provider-${VERSION}" \
  "$DEST"

rm -f "$TMP"

cd "$DEST/server"

deno install --allow-scripts=npm:canvas --frozen
