#!/usr/bin/env bash

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_ROOT="$(cd "$APP_DIR/.." && pwd)"

VENV="$PROJECT_ROOT/.venv"

if [ ! -d "$VENV" ]; then
    echo "[ERROR] Virtual environment not found:"
    echo "$VENV"
    exit 1
fi

source "$VENV/bin/activate"

cd "$APP_DIR"

echo "================================"
echo " Environment"
echo "================================"

echo "Python : $(which python)"
echo "Venv   : $VIRTUAL_ENV"

echo
echo "================================"
echo " Python syntax check"
echo "================================"

python -m compileall -q \
    main.py \
    core \
    connections \
    devices \
    services \
    gui

echo "[PASS] Python syntax"

echo
echo "================================"
echo " Import check"
echo "================================"

python - <<'PY'
from PySide6.QtWidgets import QApplication

from connections.ssh_manager import SSHManager
from core.base_device import BaseDevice
from core.base_test import BaseTest
from core.test_result import TestResult, TestStatus
from core.registry import DeviceRegistry
from gui.main_window import MainWindow

print("[PASS] PySide6 import OK")
print("[PASS] SSHManager import OK")
print("[PASS] Core imports OK")
print("[PASS] GUI import OK")
PY

echo
echo "================================"
echo " CHECK PASSED"
echo "================================"
