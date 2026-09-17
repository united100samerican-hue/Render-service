#!/bin/sh
set -eu

VERSION="2.0.0"
DEST="${POT_PROVIDER_HOME:-/opt/bgutil-ytdlp-pot-provider}"
TMP="/tmp/bgutil-ytdlp-pot-provider-${VERSION}.tar.gz"
URL="https://github.com/Brainicism/bgutil-ytdlp-pot-provider/archive/refs/tags/${VERSION}.tar.gz"

command -v curl >/dev/null 2>&1 || { echo "curl is required" >&2; exit 1; }
command -v deno >/dev/null 2>&1 || { echo "deno is required" >&2; exit 1; }

rm -rf "$DEST" "/opt/bgutil-ytdlp-pot-provider-${VERSION}" "$TMP"
mkdir -p /opt

echo "Installing bgutil-ytdlp-pot-provider ${VERSION}"
curl --retry 4 --retry-delay 2 --retry-all-errors -fsSL "$URL" -o "$TMP"
tar -xzf "$TMP" -C /opt
mv "/opt/bgutil-ytdlp-pot-provider-${VERSION}" "$DEST"
rm -f "$TMP"

cd "$DEST/server"
deno install --allow-scripts=npm:canvas --frozen

test -f "$DEST/server/src/main.ts"
test -d "$DEST/server/node_modules"

echo "bgutil-ytdlp-pot-provider ${VERSION} installed at $DEST"
