#!/bin/sh
# One-time setup: build a venv holding Kokoro + torch, and warm the model
# cache so the first spoken reply isn't a download.
set -e
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
HOME_DIR=${LOCAL_TTS_HOME:-$HOME/.local/share/local-tts}
VENV="$HOME_DIR/venv"

echo "==> local-tts setup"
echo "    runtime home: $HOME_DIR"

if ! command -v espeak-ng >/dev/null 2>&1; then
  echo "!!  espeak-ng not found. Kokoro needs it for phonemization."
  case "$(uname -s)" in
    Darwin) echo "    install with:  brew install espeak-ng" ;;
    *)      echo "    install with:  sudo apt-get install espeak-ng" ;;
  esac
  exit 1
fi

PY=${PYTHON:-python3}
mkdir -p "$HOME_DIR"

if [ ! -x "$VENV/bin/python" ]; then
  echo "==> creating venv"
  "$PY" -m venv "$VENV"
fi

echo "==> installing dependencies (this pulls torch, ~1-2 GB)"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$DIR/requirements.txt"

echo "==> downloading the Kokoro model"
"$VENV/bin/python" - <<'PYEOF'
from kokoro import KPipeline
KPipeline(lang_code="a")
print("    model cached")
PYEOF

echo "==> config"
"$VENV/bin/python" "$DIR/scripts/config.py" status

echo
echo "Done. Try it:   $DIR/scripts/tts-ctl.sh say 'Setup complete.'"
