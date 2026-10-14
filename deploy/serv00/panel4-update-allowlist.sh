#!/bin/sh
set -eu

root=/home/meatroll/bin
state="$root/gemini-panel4-ip.state"
key=/home/meatroll/.ssh/id_ed25519_gemini_allowlist
host=igsshan@s2.serv00.com
remote_command=/home/igsshan/bin/update-panel4-allowlist

get_ip() {
    /usr/local/bin/curl -fsS --max-time 8 "$1"
}

ip1=$(get_ip https://api.ipify.org) || exit 0
ip2=$(get_ip https://ifconfig.me/ip) || exit 0
[ "$ip1" = "$ip2" ] || exit 0
case "$ip1" in
    *[!0-9.]*|'') exit 0 ;;
esac

old=$(cat "$state" 2>/dev/null || true)
[ "$old" = "$ip1" ] && exit 0

printf '%s\n' "$ip1" | /usr/bin/ssh -T -o BatchMode=yes -o IdentitiesOnly=yes \
    -o ConnectTimeout=10 -o ServerAliveInterval=20 -o ServerAliveCountMax=3 \
    -i "$key" "$host" || exit 1

printf '%s\n' "$ip1" > "$state"
chmod 600 "$state"
