#!/usr/bin/env bash

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_ROOT="$(cd "$APP_DIR/.." && pwd)"

VENV="$PROJECT_ROOT/.venv"

echo "================================"
echo " Hardware Test Application"
echo "================================"
echo "Project : $PROJECT_ROOT"
echo "App     : $APP_DIR"
echo "Venv    : $VENV"
echo

if [ ! -d "$VENV" ]; then
    echo "[ERROR] Virtual environment not found:"
    echo "$VENV"
    echo
    echo "Create it with:"
    echo "cd $PROJECT_ROOT"
    echo "python3 -m venv .venv"
    echo "source .venv/bin/activate"
    echo "python -m pip install -r desktop_app/requirements.txt"
    exit 1
fi

source "$VENV/bin/activate"

cd "$APP_DIR"

echo "[INFO] Python: $(which python)"
echo "[INFO] Starting application..."
echo

python main.py
