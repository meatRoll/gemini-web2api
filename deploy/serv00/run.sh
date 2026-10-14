#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$root"
exec "$root/.venv/bin/python" -m gemini_web2api --config "$root/config.json" "$@"
