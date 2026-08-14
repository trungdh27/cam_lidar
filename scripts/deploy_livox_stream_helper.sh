#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "Usage: $0 user@jetson-ip" >&2
    exit 1
fi

TARGET="$1"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE="$PROJECT_DIR/jetson_tools/livox_stream"
REMOTE_SOURCE_ROOT=".cam_lidar_src"

for command_name in ssh scp; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "[ERROR] Required Host command is missing: $command_name" >&2
        exit 1
    fi
done

for required_file in CMakeLists.txt main.cpp; do
    if [[ ! -f "$SOURCE/$required_file" ]]; then
        echo "[ERROR] Missing stream helper source: $SOURCE/$required_file" >&2
        exit 1
    fi
done

echo "[1/4] Checking Jetson aarch64 and Livox SDK2 prerequisites on $TARGET"
ssh "$TARGET" 'bash -s' <<'REMOTE_PREFLIGHT'
set -euo pipefail

architecture="$(uname -m)"
if [[ "$architecture" != "aarch64" && "$architecture" != "arm64" ]]; then
    echo "[ERROR] Expected Jetson aarch64, got: $architecture" >&2
    exit 1
fi
for command_name in cmake c++ readelf; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "[ERROR] Required build command is missing: $command_name" >&2
        exit 1
    fi
done
for header in livox_lidar_api.h livox_lidar_def.h; do
    if [[ ! -r "/usr/local/include/$header" ]]; then
        echo "[ERROR] Missing Livox SDK2 header: /usr/local/include/$header" >&2
        exit 1
    fi
done

sdk_library="$(
    find /usr/local/lib /usr/local/lib64 /usr/lib/aarch64-linux-gnu /usr/lib \
        -maxdepth 1 \( \
            -name 'liblivox_lidar_sdk_shared.so' -o \
            -name 'liblivox_lidar_sdk_shared.so.*' -o \
            -name 'liblivox_lidar_sdk_static.a' \
        \) -print -quit 2>/dev/null || true
)"
if [[ -z "$sdk_library" ]] && command -v ldconfig >/dev/null 2>&1; then
    sdk_library="$(
        ldconfig -p 2>/dev/null \
            | awk '/liblivox_lidar_sdk_shared\.so/ && !found {print $NF; found=1}'
    )"
fi
if [[ -z "$sdk_library" || ! -r "$sdk_library" ]]; then
    echo "[ERROR] Livox SDK2 library was not found or is not readable." >&2
    exit 1
fi

mkdir -p "$HOME/.cam_lidar_src" "$HOME/.cam_lidar/bin"
rm -rf "$HOME/.cam_lidar_src/livox_stream"
printf '%s\n' "$sdk_library" > "$HOME/.cam_lidar_src/livox_sdk_library.path"
echo "[OK] Architecture: $architecture"
echo "[OK] Livox SDK2 library: $sdk_library"
REMOTE_PREFLIGHT

echo "[2/4] Copying stream helper source to $TARGET"
scp -r "$SOURCE" "$TARGET:~/$REMOTE_SOURCE_ROOT/"

echo "[3/4] Configuring and building stream helper natively on Jetson"
ssh "$TARGET" 'bash -s' <<'REMOTE_BUILD'
set -euo pipefail

source_dir="$HOME/.cam_lidar_src/livox_stream"
build_dir="$source_dir/build"
binary="$build_dir/livox_stream"
destination="$HOME/.cam_lidar/bin/livox_stream"
temporary_destination="$destination.tmp.$$"
sdk_library="$(cat "$HOME/.cam_lidar_src/livox_sdk_library.path")"
trap 'rm -f "$temporary_destination"' EXIT

cmake -S "$source_dir" -B "$build_dir" \
    -DCMAKE_BUILD_TYPE=Release \
    -DLIVOX_SDK_LIBRARY="$sdk_library"
cmake --build "$build_dir" --parallel "$(nproc)"

if [[ ! -s "$binary" || ! -x "$binary" ]]; then
    echo "[ERROR] Build did not produce an executable: $binary" >&2
    exit 1
fi
if ! readelf -h "$binary" | grep -E 'Machine:[[:space:]]+AArch64' >/dev/null; then
    echo "[ERROR] Built stream helper is not AArch64." >&2
    exit 1
fi
if ldd "$binary" 2>&1 | grep 'not found' >/dev/null; then
    echo "[ERROR] Stream helper has unresolved libraries:" >&2
    ldd "$binary" >&2 || true
    exit 1
fi

cp "$binary" "$temporary_destination"
chmod +x "$temporary_destination"
"$temporary_destination" --version
mv -f "$temporary_destination" "$destination"
trap - EXIT
REMOTE_BUILD

echo "[4/4] Verifying installed stream helper"
ssh "$TARGET" 'test -x "$HOME/.cam_lidar/bin/livox_stream"'
ssh "$TARGET" '"$HOME/.cam_lidar/bin/livox_stream" --version'
echo "[OK] Livox stream helper installed at ~/.cam_lidar/bin/livox_stream"
