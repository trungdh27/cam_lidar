#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

if [[ -f ".venv/bin/activate" ]]; then
    source ".venv/bin/activate"
fi

echo "[1/4] Python syntax..."
python -m compileall -q core devices desktop_app

echo "[2/4] Core imports..."
python - <<'PY'
from core.remote.ssh_manager import SSHManager, SSHConfig
from core.network.network_manager import NetworkManager
from core.network.temporary_ip_manager import TemporaryIPManager
from devices.livox.sdk2_backend import LivoxSDK2Backend
print("Core imports OK")
PY

echo "[3/4] UI imports..."
python - <<'PY'
from desktop_app.ui.lidar_page import LidarPage
from desktop_app.ui.camera_page import CameraPage
from desktop_app.ui.main_window import MainWindow
from desktop_app.workers.camera_worker import CameraActionWorker
from desktop_app.workers.camera_discovery_worker import CameraDiscoveryWorker
from desktop_app.workers.camera_connection_worker import CameraConnectionWorker
from desktop_app.workers.livox_discovery_worker import LivoxDiscoveryWorker
from devices.camera.service import CameraService
from devices.camera.zed_adapter import ZedAdapter
from devices.camera.remote_zed_adapter import RemoteZedAdapter
print("UI imports OK")
PY

echo "[4/4] Shell scripts..."
bash -n scripts/run.sh
bash -n scripts/deploy_livox_helper.sh

echo "[OK] Source verification passed."
