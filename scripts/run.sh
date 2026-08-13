#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "================================"
echo " Hardware Test Automation"
echo "================================"
echo "Project : $PROJECT_DIR"

if [[ ! -f "$PROJECT_DIR/.venv/bin/activate" ]]; then
    echo "[ERROR] Virtual environment not found: $PROJECT_DIR/.venv"
    exit 1
fi

source "$PROJECT_DIR/.venv/bin/activate"
cd "$PROJECT_DIR"

echo "[INFO] Python: $(which python)"
echo "[INFO] Starting application..."
echo
python -m desktop_app.main
