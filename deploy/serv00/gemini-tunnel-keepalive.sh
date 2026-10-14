#!/bin/sh
set -eu

root=/home/igsshan/bin
binary=/tmp/igsshan-cloudflared
tmp_binary="$binary.new"
pidfile="$root/gemini-tunnel.pid"
logfile="$root/gemini-tunnel.log"
config="$root/gemini-tunnel.yml"

if [ ! -x "$binary" ] || [ "$root/cloudflared" -nt "$binary" ]; then
    cp "$root/cloudflared" "$tmp_binary"
    chmod 700 "$tmp_binary"
    mv "$tmp_binary" "$binary"
fi

if pgrep -f "$binary tunnel.*gemini-panel2" >/dev/null 2>&1; then
    exit 0
fi

nohup "$binary" tunnel --config "$config" run gemini-panel2 \
    >> "$logfile" 2>&1 < /dev/null &
printf '%s\n' "$!" > "$pidfile"
