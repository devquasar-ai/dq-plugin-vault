#!/bin/sh
# Control surface for session summary cards (the /session-db:cards command).
exec python3 "$(dirname "$0")/sessiondb.py" cards-ctl "$@"
