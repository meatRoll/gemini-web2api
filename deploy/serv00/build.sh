#!/bin/sh
set -eu
export COPYFILE_DISABLE=1

root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
out=${1:-"$root/../gemini-web2api-serv00.tar.gz"}
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT HUP INT TERM

mkdir -p "$stage/gemini-web2api/gemini_web2api"
cp "$root"/gemini_web2api/*.py "$stage/gemini-web2api/gemini_web2api/"
cp "$root/requirements.txt" "$stage/gemini-web2api/"
cp "$root/deploy/serv00/config.example.json" "$stage/gemini-web2api/"
cp "$root/deploy/serv00/run.sh" "$stage/gemini-web2api/"
cp "$root/deploy/serv00/start.sh" "$stage/gemini-web2api/"
cp "$root/deploy/serv00/keepalive.sh" "$stage/gemini-web2api/"
cp "$root/deploy/serv00/README.md" "$stage/gemini-web2api/"

tar -czf "$out" -C "$stage" gemini-web2api
printf 'Created %s\n' "$out"
