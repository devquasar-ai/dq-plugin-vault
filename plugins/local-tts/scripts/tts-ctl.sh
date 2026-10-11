#!/bin/sh
# Control surface for the local Kokoro TTS daemon.
set -e
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
HOME_DIR=${LOCAL_TTS_HOME:-$HOME/.local/share/local-tts}
PY="$HOME_DIR/venv/bin/python"
[ -x "$PY" ] || PY=$(command -v python3)

cmd=${1:-status}
shift 2>/dev/null || true

case "$cmd" in
  on)       "$PY" "$DIR/config.py" set enabled true  && echo "TTS on" ;;
  off)      "$PY" "$DIR/config.py" set enabled false && "$PY" "$DIR/speak.py" --stop >/dev/null 2>&1 || true
            echo "TTS off" ;;
  stop)     "$PY" "$DIR/speak.py" --stop >/dev/null 2>&1 || true ;;
  say)      "$PY" "$DIR/speak.py" "$@" ;;
  voice)    if [ -n "$1" ]; then "$PY" "$DIR/config.py" set voice "$*" && echo "voice: $*"
            else "$PY" "$DIR/config.py" voices; fi ;;
  speed)    "$PY" "$DIR/config.py" set speed "$1" && echo "speed: $1" ;;
  backend)  if [ -n "$1" ]; then "$PY" "$DIR/config.py" set backend "$1" && echo "backend: $1"
            else "$PY" "$DIR/config.py" get | grep -E '"(backend|server_url|say_voice)"'; fi ;;
  set)      "$PY" "$DIR/config.py" set "$@" ;;
  start)    "$PY" "$DIR/speak.py" --start ;;
  restart)  "$PY" "$DIR/speak.py" --shutdown >/dev/null 2>&1 || true
            "$PY" "$DIR/speak.py" --start ;;
  shutdown) "$PY" "$DIR/speak.py" --shutdown ;;
  log)      tail -n "${1:-40}" "$HOME_DIR/daemon.log" ;;
  server)   "$DIR/server-ctl.sh" "$@" ;;
  status)   "$PY" "$DIR/config.py" status ;;
  *)        echo "usage: tts-ctl.sh {on|off|stop|say TEXT|voice [NAME]|speed N|backend [embedded|http|say]|server up|down|status|set K V|start|restart|shutdown|status|log [N]}" >&2
            exit 2 ;;
esac
