#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"

echo "Hardware Test Automation - Ubuntu host setup"
echo "Project: $PROJECT_DIR"

if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    source /etc/os-release
else
    ID="unknown"
    VERSION_ID="unknown"
fi

if [[ "${ID:-}" == "ubuntu" && "${VERSION_ID:-}" == "22.04" ]]; then
    echo "[PASS] OS: Ubuntu 22.04"
else
    echo "[WARN] Expected Ubuntu 22.04; detected ${PRETTY_NAME:-unknown}."
    echo "       Continuing because the package names may still work on this host."
fi

if [[ "$(uname -m)" != "x86_64" ]]; then
    echo "[WARN] Host architecture is $(uname -m); x86_64 Ubuntu is the normal development host."
fi

if ! command -v apt-get >/dev/null 2>&1; then
    echo "[ERROR] apt-get is required for host package installation." >&2
    exit 1
fi
if ! command -v sudo >/dev/null 2>&1; then
    echo "[ERROR] sudo is required to install Ubuntu packages." >&2
    exit 1
fi

APT_PACKAGES=(
    git
    python3
    python3-pip
    python3-venv
    python3-dev
    build-essential
    cmake
    pkg-config
    openssh-client
    curl
    rsync
    libxcb-cursor0
    libxcb-xinerama0
    libxcb-icccm4
    libxcb-image0
    libxcb-keysyms1
    libxcb-render-util0
    libxkbcommon-x11-0
    libgl1
    libegl1
    iproute2
    iputils-ping
    iw
    network-manager
    iperf3
    bluez
    alsa-utils
    pulseaudio-utils
    sox
    ffmpeg
    stress-ng
    sysstat
)

echo "[INFO] Installing core host packages and commonly used hardware tools..."
sudo env DEBIAN_FRONTEND=noninteractive apt-get update
sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y "${APT_PACKAGES[@]}"

if [[ -f "$VENV_DIR/bin/activate" ]]; then
    echo "[PASS] Preserving existing virtual environment: $VENV_DIR"
elif [[ -e "$VENV_DIR" ]]; then
    echo "[ERROR] $VENV_DIR exists but is not a usable Python virtual environment." >&2
    echo "        Repair or move it manually; this script will not delete project data." >&2
    exit 1
else
    echo "[INFO] Creating virtual environment: $VENV_DIR"
    python3 -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
cd "$PROJECT_DIR"

echo "[INFO] Python: $(python --version)"
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
python -m pip install -r requirements-dev.txt

echo
echo "[PASS] Host setup completed."
echo "Next steps:"
echo "  source $VENV_DIR/bin/activate"
echo "  ./scripts/check_environment.sh"
echo "  ./scripts/run.sh"
echo
echo "Vendor SDKs, ROS, CUDA, and Jetson-specific software were not installed."
