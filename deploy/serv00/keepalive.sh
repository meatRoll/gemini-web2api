#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec flock -n "$root/.keepalive.lock" sh "$root/start.sh"
