#!/bin/sh
set -eu
root=/home/meatroll/bin
pidfile="$root/gemini-private-relay.pid"
logfile="$root/gemini-private-relay.log"
port=7375

healthy() {
    code=$(/usr/local/bin/curl -sS --max-time 10 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/v1/models" 2>/dev/null || true)
    [ "$code" = 401 ]
}

if [ -f "$pidfile" ]; then
    pid=$(cat "$pidfile")
    case "$pid" in ''|*[!0-9]*) pid=0 ;; esac
    if ps -p "$pid" -o command= 2>/dev/null | grep -q 'socat.*TCP-LISTEN:7375'; then
        if healthy; then exit 0; fi
        kill "$pid" 2>/dev/null || true
        sleep 1
    fi
fi

umask 077
nohup /usr/local/bin/socat -T 240 \
    TCP-LISTEN:7375,bind=127.0.0.1,reuseaddr,fork,max-children=3 \
    EXEC:'/usr/bin/ssh -T -o BatchMode=yes -o IdentitiesOnly=yes -o ConnectTimeout=10 -o ServerAliveInterval=20 -o ServerAliveCountMax=3 -i /home/meatroll/.ssh/id_ed25519_gemini_panel2_stream igsshan@s2.serv00.com',nofork \
    >> "$logfile" 2>&1 < /dev/null &
pid=$!
printf '%s\n' "$pid" > "$pidfile"
n=0
while [ "$n" -lt 10 ]; do
    if healthy; then
        printf 'gemini relay ready: pid=%s port=%s\n' "$pid" "$port"
        exit 0
    fi
    if ! kill -0 "$pid" 2>/dev/null; then break; fi
    n=$((n + 1))
    sleep 1
done
printf 'gemini relay failed; see %s\n' "$logfile" >&2
exit 1
