#!/usr/bin/env bash
set -u -o pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"
REQUIRED_FAILURES=0

printf '%s\n\n' 'Hardware Test Automation - Environment Check'
printf '%-18s : %-22s %s\n' 'Project' "$PROJECT_DIR" 'INFO'

pass_check() {
    printf '%-18s : %-22s %s\n' "$1" "$2" 'PASS'
}

fail_check() {
    printf '%-18s : %-22s %s\n' "$1" "$2" 'FAIL'
    REQUIRED_FAILURES=$((REQUIRED_FAILURES + 1))
}

warn_check() {
    printf '%-18s : %-22s %s\n' "$1" "$2" 'WARN'
}

optional_check() {
    printf '%-18s : %-22s %s\n' "$1" "$2" 'OPTIONAL'
}

dev_check() {
    printf '%-18s : %-22s %s\n' "$1" "$2" 'DEV'
}

check_required_command() {
    local label="$1"
    local command_name="$2"
    if command -v "$command_name" >/dev/null 2>&1; then
        pass_check "$label" "$(command -v "$command_name")"
    else
        fail_check "$label" 'not installed'
    fi
}

check_optional_command() {
    local label="$1"
    local command_name="$2"
    if command -v "$command_name" >/dev/null 2>&1; then
        pass_check "$label" "$(command -v "$command_name")"
    else
        optional_check "$label" 'not detected'
    fi
}

check_required_import() {
    local label="$1"
    local module_name="$2"
    if "$VENV_PYTHON" -c "import $module_name" >/dev/null 2>&1; then
        pass_check "$label" 'installed'
    else
        fail_check "$label" 'Python import failed'
    fi
}

if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    source /etc/os-release
fi
if [[ "${ID:-}" == 'ubuntu' && "${VERSION_ID:-}" == '22.04' ]]; then
    pass_check 'OS' 'Ubuntu 22.04'
else
    warn_check 'OS' "${PRETTY_NAME:-unknown} (Ubuntu 22.04 expected)"
fi

architecture="$(uname -m 2>/dev/null || printf '%s' unknown)"
if [[ "$architecture" == 'x86_64' ]]; then
    pass_check 'Architecture' "$architecture"
else
    warn_check 'Architecture' "$architecture (x86_64 host expected)"
fi

echo
echo 'Required host tools:'
check_required_command 'Python' python3
check_required_command 'Git' git
check_required_command 'SSH client' ssh

if [[ -x "$VENV_PYTHON" && -f "$PROJECT_DIR/.venv/bin/activate" ]]; then
    python_version="$($VENV_PYTHON --version 2>&1)"
    pass_check 'Virtualenv' "$PROJECT_DIR/.venv"
    pass_check 'Venv Python' "$python_version"
else
    fail_check 'Virtualenv' "$PROJECT_DIR/.venv missing or incomplete"
fi

if command -v python3 >/dev/null 2>&1; then
    if python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
        pass_check 'Python version' "$(python3 --version 2>&1)"
    else
        fail_check 'Python version' 'Python 3.10+ required'
    fi
fi

echo
echo 'Required Python packages (.venv):'
if [[ -x "$VENV_PYTHON" ]]; then
    check_required_import 'PySide6' PySide6
    check_required_import 'asyncssh' asyncssh
    check_required_import 'PyYAML' yaml
    check_required_import 'pydantic' pydantic
    check_required_import 'numpy' numpy
    check_required_import 'pyqtgraph' pyqtgraph
    check_required_import 'PyOpenGL' OpenGL
    check_required_import 'openpyxl' openpyxl
else
    echo '  Python imports skipped because the virtual environment is unavailable.'
fi

echo
echo 'Development tools:'
if [[ -x "$VENV_PYTHON" ]] && "$VENV_PYTHON" -m pytest --version >/dev/null 2>&1; then
    pass_check 'pytest' "$($VENV_PYTHON -m pytest --version 2>&1 | head -n 1)"
else
    dev_check 'pytest' 'missing; install requirements-dev.txt'
fi

echo
echo 'Hardware command-line tools:'
check_optional_command 'pactl' pactl
check_optional_command 'aplay' aplay
check_optional_command 'arecord' arecord
check_optional_command 'amixer' amixer
check_optional_command 'bluetoothctl' bluetoothctl
check_optional_command 'btmgmt' btmgmt
check_optional_command 'iw' iw
check_optional_command 'nmcli' nmcli
check_optional_command 'iperf3' iperf3
check_optional_command 'stress-ng' stress-ng
check_optional_command 'mpstat' mpstat

echo
echo 'Optional vendor/software dependencies:'
check_optional_command 'ROS 2' ros2
if command -v ZED_Explorer >/dev/null 2>&1 || [[ -d /usr/local/zed ]]; then
    pass_check 'ZED SDK' 'detected'
else
    optional_check 'ZED SDK' 'not detected'
fi
if command -v realsense-viewer >/dev/null 2>&1 || command -v rs-enumerate-devices >/dev/null 2>&1; then
    pass_check 'RealSense tools' 'detected'
else
    optional_check 'RealSense tools' 'not detected'
fi
if [[ -r /usr/local/include/livox_lidar_api.h ]] && \
   [[ -r /usr/local/include/livox_lidar_def.h ]]; then
    pass_check 'Livox SDK2' 'headers detected'
else
    optional_check 'Livox SDK2' 'not detected'
fi

echo
if [[ "$REQUIRED_FAILURES" -eq 0 ]]; then
    echo '[PASS] Required core environment is available.'
    echo '[INFO] OPTIONAL hardware/vendor entries do not block this result.'
    exit 0
fi

echo "[FAIL] Required core checks failed: $REQUIRED_FAILURES"
echo '       Run ./scripts/setup_host.sh, then run this check again.'
exit 1
