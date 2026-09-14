# Device domain architecture

This project keeps hardware test behavior inside the domain that owns it.
Camera and LiDAR have independent definitions, parameters, runners, results,
evidence, and history ownership. LiDAR history is a read-only view over its
evidence; Camera history remains future work. A device domain may depend on
shared infrastructure; device domains must not depend on one another.

```text
                       Application composition / UI
                                  |
              +-------------------+-------------------+
              |                                       |
       DeviceRegistry                         Jetson/SSH/network
       summary contract                      shared infrastructure
              ^                                       ^
              |                                       |
      +-------+-------+                       +-------+-------+
      |               |                       |               |
 Camera domain   LiDAR domain  --------------+---------------+
      |               |
 camera tests     lidar tests
 camera evidence  lidar evidence
```

The diagram shows both domains publishing high-level state to the registry and
using shared infrastructure. There is no Camera-to-LiDAR dependency.

## Shared infrastructure

Shared code is limited to capabilities that are hardware-domain neutral:

- `JetsonState` and `JetsonConnectionService`
- `SSHManager` and network utilities
- `DeviceRegistry`
- common UI widgets, navigation, and application logging
- the `MainWindow` composition root

`DeviceRegistry` contains only application summaries: device, family, model,
serial, availability, status, last update, and last error. It must not contain
FPS calculations, LiDAR point-rate or packet-loss calculations, IMU jitter,
CAN bitrate evaluation, or EtherCAT PDO details. Dashboard code consumes this
summary contract and does not inspect device implementations.

## Camera domain

Camera profiles, adapters, services, runtime behavior, tests, results, and
evidence are Camera-owned. The public test boundary is
`devices.camera.testing`. Its implementation currently includes the legacy
`core.testing` modules and the Camera test worker; keeping those paths in Phase
0 avoids changing the working runner.

Camera structured results are written below:

```text
evidence/camera/<session>/<device>/<test_id>/
```

Camera measurement schemas remain Camera-specific, for example configured and
actual FPS, frame intervals, and dropped frames.

## LiDAR domain

Livox profiles, SDK2 discovery, streaming, runtime metrics, tests, results, and
evidence are LiDAR-owned. The public test boundary is
`devices.livox.testing`, which physically owns the definitions, runner, result,
evidence, and history implementations. `desktop_app.testing` contains only
compatibility re-exports for legacy callers; new production code must use the
LiDAR boundary.

LiDAR structured results are written below:

```text
evidence/lidar/<session_id>/<test_id>/
```

LiDAR measurement schemas remain LiDAR-specific, including point rate, point
packet rate, IMU rate, and packet loss.

## Future device domains

IMU, CAN, and EtherCAT will each own their profiles, runtime, testing, results,
evidence, and history. Phase 0 creates no placeholder implementations or global
test runner for them.

## Dependency rules

Allowed:

```text
desktop_app.ui.camera_page -> Camera domain -> shared infrastructure
desktop_app.ui.lidar_page  -> LiDAR domain  -> shared infrastructure
device domain              -> DeviceRegistry -> Dashboard
MainWindow                 -> all domains (composition only)
```

Forbidden:

```text
Camera domain       -> LiDAR domain
LiDAR domain        -> Camera domain
shared infrastructure -> Camera or LiDAR implementation
Dashboard           -> device test/runtime internals
```

A future common test summary may expose only neutral fields such as device,
session ID, test ID, status, start/end timestamps, and duration. Domain
measurements and acceptance criteria must not be forced into a global schema.

## Result, evidence, and history ownership

Each domain owns its result schema and storage namespace. Camera and LiDAR
evidence paths cannot be mixed. Existing evidence is not migrated. LiDAR
history reads `evidence/lidar` without duplicating result data, and tolerates
legacy sessions without a manifest. Future Camera history must retain its own
ownership. Shared UI may display summaries without interpreting domain data.

## Family-level reuse

Reuse is allowed within a device family. ZED X One, ZED X, and ZED X Mini may
share ZED-family adapters and helpers while retaining model profiles. MID360
and MID360S may share Livox SDK2 discovery, protocol, and stream parsing while
retaining model-specific profiles and thresholds. Reuse across Camera, LiDAR,
IMU, CAN, or EtherCAT test logic is forbidden.
