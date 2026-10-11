#!/bin/bash
# Hook entry point: index new transcript lines, then update any cards that are due
# (only if cards were turned on with `cards-setup`), in the background so the
# session never waits. Overlapping runs are skipped by the indexer's locks.
# SESSION_DB_CHILD is set on the `claude -p` calls that generate cards, so those
# never trigger another round.
[ -n "$SESSION_DB_CHILD" ] && exit 0
nohup python3 "$(dirname "$0")/sessiondb.py" index -q --cards >/dev/null 2>&1 &
exit 0
