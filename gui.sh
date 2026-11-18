#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# Activate venv if present
if [ -d ".venv" ]; then
  if [ -f ".venv/bin/activate" ]; then source .venv/bin/activate; elif [ -f ".venv/Scripts/activate" ]; then source .venv/Scripts/activate; fi
elif [ -d "clip-watcher/.venv" ]; then
  if [ -f "clip-watcher/.venv/bin/activate" ]; then source clip-watcher/.venv/bin/activate; elif [ -f "clip-watcher/.venv/Scripts/activate" ]; then source clip-watcher/.venv/Scripts/activate; fi
fi

PYTHON_BIN="python3"
if ! command -v python3 >/dev/null 2>&1; then
  if command -v python >/dev/null 2>&1; then PYTHON_BIN="python"; fi
fi

export PATH="$(pwd)/ffmpeg/ffmpeg-9.0.2-essentials_build/bin:$(pwd)/clip-watcher/.venv/Scripts:$(pwd)/.venv/Scripts:$PATH"

echo "Opening http://localhost:5000 ..."
if command -v xdg-open >/dev/null 2>&1; then
  xdg-open "http://localhost:5000" &
elif command -v open >/dev/null 2>&1; then
  open "http://localhost:5000" &
fi

exec "$PYTHON_BIN" gui.py
