#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "Usage: $0 user@jetson-ip"
    exit 1
fi

TARGET="$1"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE="$PROJECT_DIR/jetson_tools/livox_discovery"

echo "Deploying Livox helper to $TARGET"
ssh "$TARGET" 'rm -rf ~/.cam_lidar_src/livox_discovery; mkdir -p ~/.cam_lidar_src ~/.cam_lidar/bin'
scp -r "$SOURCE" "$TARGET":~/.cam_lidar_src/

ssh "$TARGET" '
set -e
SRC="$HOME/.cam_lidar_src/livox_discovery"
BUILD="$SRC/build"
cmake -S "$SRC" -B "$BUILD"
cmake --build "$BUILD" -j"$(nproc)"
cp "$BUILD/livox_discover" "$HOME/.cam_lidar/bin/livox_discover"
chmod +x "$HOME/.cam_lidar/bin/livox_discover"
"$HOME/.cam_lidar/bin/livox_discover" --version
'

echo "[OK] Livox helper deployed."
