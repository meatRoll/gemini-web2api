#!/bin/sh
set -eu
exec /usr/local/bin/flock -n /home/meatroll/bin/.gemini-private-relay.lock /bin/sh /home/meatroll/bin/gemini-private-relay.sh
