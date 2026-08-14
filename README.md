# Hardware Test Automation — LiDAR MVP v0.1.0

Full source package for the current LiDAR automation foundation.

## Architecture

```text
Host Ubuntu / PySide6
        |
        | SSH
        v
Jetson
  |- Network inspection
  |- Temporary LiDAR-side IPv4
  |- Livox SDK2 native helper
  |
  `-- Ethernet --> MID-360 / MID-360S
```

## Included

- Desktop PySide6 UI
- Device Overview button/dialog
- Network Summary button/dialog
- Network Protocol button/dialog
- Live Monitor parameter/value table
- Test Suites and connection test list UI
- Live Log
- `SSHManager`
- `NetworkManager`
- Validated fixed Livox network profile
- Livox SDK2 Python backend
- Livox native C++ discovery helper for Jetson
- MID-360 / MID-360S identification
- CLI verification scripts
- Jetson helper deployment script

## Not implemented yet

The UI intentionally does not fake data. These remain placeholders until later milestones:

- point-cloud / IMU stream monitor backend
- packet-loss calculation
- firmware/internal parameter reader
- real ping measurement
- automatic TestEngine execution
- evidence/history persistence
- ROS2/RViz integration

## Replace an existing project safely

This package includes `INSTALL_REPLACE.sh`.

It replaces only source directories/files and preserves:

- `.git`
- `.venv`
- `data`
- `evidence`

Example:

```bash
chmod +x INSTALL_REPLACE.sh
./INSTALL_REPLACE.sh ~/cam_lidar
```

Then:

```bash
cd ~/cam_lidar
source .venv/bin/activate
python -m pip install -r requirements.txt
./scripts/verify_source.sh
./scripts/run.sh
```

## Livox helper on Jetson

Livox SDK2 must already be installed on Jetson.

Check:

```bash
ls /usr/local/include/livox_lidar_api.h
ls /usr/local/include/livox_lidar_def.h
ls /usr/local/lib/liblivox_lidar_sdk_*
```

Deploy the helper from Host Ubuntu:

```bash
./scripts/deploy_livox_helper.sh huu@192.168.9.169
```

Then verify:

```bash
ssh huu@192.168.9.169 '~/.cam_lidar/bin/livox_discover --version'
```

Run fixed-profile discovery directly on Jetson:

```bash
~/.cam_lidar/bin/livox_discover \
  --host-ip 192.168.1.5 \
  --expected-lidar-ip 192.168.1.162 \
  --model MID360 \
  --timeout 8
```

## Normal UI flow

```text
CONNECT
  -> inspect Jetson networking
VALIDATE FIXED PROFILE
  -> compare the Jetson NIC and IPv4 address without changing them
AUTO DISCOVER
  -> Livox SDK2 helper on Jetson
  -> model / serial / LiDAR IP
```

Use the three detail buttons as needed:

- Device Overview
- Network Summary
- Network Protocol
