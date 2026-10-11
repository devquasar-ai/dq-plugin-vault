#!/bin/sh
# Control surface for /dq-session-db:cleanup.
S="$(dirname "$0")/sessiondb.py"
verb=${1:-list}
shift 2>/dev/null || true
case "$verb" in
  list)   exec python3 "$S" cleanup "$@" ;;
  hide)   exec python3 "$S" cleanup --hide "$@" ;;
  remove) exec python3 "$S" cleanup --remove "$@" ;;
  unhide) exec python3 "$S" unhide "$@" ;;
  auto)   exec python3 "$S" cleanup-auto "$@" ;;
  *) echo "usage: cleanup-ctl.sh [list | hide [SESSION...] | remove [--yes] [SESSION...] | unhide SESSION... | auto on|off]" >&2; exit 2 ;;
esac
