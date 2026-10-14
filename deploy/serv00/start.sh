#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$root"
umask 077
mkdir -p logs
port=$(.venv/bin/python -c 'import json; print(json.load(open("config.json"))["port"])')

healthy() {
    code=$(curl -sS --max-time 3 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/" 2>/dev/null || true)
    [ "$code" = 200 ]
}

if [ -f service.pid ]; then
    pid=$(cat service.pid)
    case "$pid" in
        ''|*[!0-9]*) pid=0 ;;
    esac
    if ps -p "$pid" -o command= 2>/dev/null | grep -q '[p]ython.*-m gemini_web2api'; then
        if healthy; then
            exit 0
        fi
        kill "$pid" 2>/dev/null || true
        sleep 2
    fi
fi

nohup sh "$root/run.sh" >> "$root/logs/service.log" 2>&1 < /dev/null &
pid=$!
printf '%s\n' "$pid" > service.pid
n=0
while [ "$n" -lt 10 ]; do
    if healthy; then
        printf 'gemini-web2api running: pid=%s port=%s\n' "$pid" "$port"
        exit 0
    fi
    if ! kill -0 "$pid" 2>/dev/null; then
        break
    fi
    n=$((n + 1))
    sleep 1
done
printf 'gemini-web2api failed to start; see %s/logs/service.log\n' "$root" >&2
exit 1
