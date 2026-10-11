#!/bin/sh
# Stop / PreToolUse hook entrypoint. Buffers the hook payload and hands it to a detached
# worker so the turn never waits on model loading or playback.
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
HOME_DIR=${LOCAL_TTS_HOME:-$HOME/.local/share/local-tts}
PY="$HOME_DIR/venv/bin/python"
[ -x "$PY" ] || PY=$(command -v python3) || exit 0

payload=$(cat)
[ -n "$payload" ] || exit 0

mkdir -p "$HOME_DIR"
printf '%s' "$payload" | nohup "$PY" "$DIR/stop_hook.py" >>"$HOME_DIR/daemon.log" 2>&1 &
exit 0
