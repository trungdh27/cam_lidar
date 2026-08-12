#!/usr/bin/env bash

set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "================================"
echo " Hardware Test Automation"
echo "================================"
echo "Project : $PROJECT_DIR"

source "$PROJECT_DIR/.venv/bin/activate"

cd "$PROJECT_DIR"

echo "[INFO] Python: $(which python)"
echo "[INFO] Starting application..."
echo

python -m desktop_app.main
