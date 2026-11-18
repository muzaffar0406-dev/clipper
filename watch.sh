#!/usr/bin/env bash
# Usage: ./watch.sh <twitch_channel> [--min N] [--factor 2.5] [--pre 20] [--post 10]
#                                    [--whisper-model base] [--no-whisper]
set -euo pipefail
cd "$(dirname "$0")"

if [ $# -lt 1 ] || [[ "$1" == -* ]]; then
  echo "Usage: $0 <twitch_channel> [--min N] [options]   (see: python watcher.py -h)" >&2
  exit 1
fi

# 1. Activate virtual environment if present
if [ -d ".venv" ]; then
  if [ -f ".venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
  elif [ -f ".venv/Scripts/activate" ]; then
    # shellcheck disable=SC1091
    source .venv/Scripts/activate
  fi
elif [ -d "clip-watcher/.venv" ]; then
  if [ -f "clip-watcher/.venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source clip-watcher/.venv/bin/activate
  elif [ -f "clip-watcher/.venv/Scripts/activate" ]; then
    # shellcheck disable=SC1091
    source clip-watcher/.venv/Scripts/activate
  fi
fi

# 2. Locate Python executable
PYTHON_BIN=""
for candidate in python3 python .venv/bin/python .venv/Scripts/python.exe clip-watcher/.venv/Scripts/python.exe; do
  if command -v "$candidate" >/dev/null 2>&1; then
    PYTHON_BIN="$candidate"
    break
  fi
done

if [ -z "$PYTHON_BIN" ]; then
  echo "Missing dependency: python3/python" >&2
  echo "Please install Python 3.10+ and dependencies." >&2
  exit 1
fi

# 3. Add local FFmpeg and venv scripts to PATH if available
SCRIPT_DIR="$(pwd)"
for extra_path in \
  "$SCRIPT_DIR/ffmpeg/ffmpeg-9.0.2-essentials_build/bin" \
  "$SCRIPT_DIR/../ffmpeg/ffmpeg-9.0.2-essentials_build/bin" \
  "$SCRIPT_DIR/.venv/Scripts" \
  "$SCRIPT_DIR/clip-watcher/.venv/Scripts" \
  "$SCRIPT_DIR/.venv/bin" \
  "$SCRIPT_DIR/clip-watcher/.venv/bin"; do
  if [ -d "$extra_path" ]; then
    export PATH="$extra_path:$PATH"
  fi
done

mkdir -p rec clips
exec "$PYTHON_BIN" watcher.py "$@"
