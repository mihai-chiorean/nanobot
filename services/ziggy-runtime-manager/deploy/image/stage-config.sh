#!/bin/sh
set -eu
umask 077
mkdir -p /run/ziggy/config
tmp=/run/ziggy/config/.config.json.tmp
cat > "$tmp"
test -s "$tmp"
chmod 0400 "$tmp"
mv -f "$tmp" /run/ziggy/config/config.json
