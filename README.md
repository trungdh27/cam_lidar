# Hardware Test Automation

## Overview

Hardware Test Automation is a PySide6 desktop application for controlling, inspecting, testing, and collecting evidence from a Jetson-based robot and its hardware interfaces. The desktop application runs on an Ubuntu host and communicates with the remote Jetson over SSH. Hardware-specific SDKs and devices are discovered at runtime; they are not assumed to be present on every development machine.

## Supported Modules

The current application contains these navigation areas and test domains:

- Dashboard
- Camera
- Audio
- AI
- LiDAR
- Bluetooth
- IMU
- CAN
- EtherCAT
- Wi-Fi
- Stress Test

Some areas are more complete than others, and hardware-dependent actions can be unavailable when the corresponding device, driver, SDK, or remote service is missing.

## Supported Environment

### Core environment

- Ubuntu 22.04 LTS on the host development machine
- Python 3.10 or newer
- Typically an x86_64 host development machine
- Git and an OpenSSH client
- SSH access from the host to the remote Jetson
- A virtual environment created at `.venv`

The remote device under test (DUT) is normally an aarch64 Ubuntu Jetson. The host and DUT do not need the same CPU architecture because the Python GUI runs on the host and hardware commands are executed remotely where applicable.

### Optional hardware/vendor environment

Camera and LiDAR SDKs, ROS 2, Jetson utilities, USB devices, network interfaces, and vendor drivers are module-specific. They are not installed by the generic host setup script. Install them only when the module and test scenario require them.

## Quick Start

From a fresh clone on Ubuntu:

```bash
git clone git@github.com:trungdh27/cam_lidar.git
cd cam_lidar

chmod +x scripts/setup_host.sh scripts/check_environment.sh scripts/run.sh
./scripts/setup_host.sh

./scripts/check_environment.sh
./scripts/run.sh
```

`setup_host.sh` installs the host packages used by the project, creates `.venv` if necessary, and installs runtime plus development dependencies. It preserves an existing `.venv` and does not remove project data.

## Manual Installation

```bash
python3 -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
python -m pip install -r requirements-dev.txt
```

`requirements-dev.txt` includes the runtime requirements and the test runner.

## Development Setup

Activate the environment in every new shell:

```bash
source .venv/bin/activate
```

Run the test suite with the interpreter from the active environment:

```bash
python -m pytest -q
```

For one test file:

```bash
python -m pytest -q tests/test_audio_manager.py
```

Using `python -m pytest` avoids accidentally using a system `pytest` command outside `.venv`.

## Jetson SSH Setup

SSH key authentication is recommended. The application can also use password authentication when configured, but no password is stored in this repository.

Test a connection manually:

```bash
ssh <JETSON_USER>@<JETSON_IP>
```

To install a public key on the Jetson:

```bash
ssh-copy-id <JETSON_USER>@<JETSON_IP>
```

To verify non-interactive key authentication:

```bash
ssh -o BatchMode=yes \
    -o PreferredAuthentications=publickey \
    <JETSON_USER>@<JETSON_IP> \
    'hostname; whoami'
```

When key authentication is configured, the application's password field is optional. Use the actual Jetson user and address for your lab; do not commit them to source or documentation.

## Module Dependencies

The generic host setup installs common command-line tools, but it does not install vendor SDKs or ROS. The following dependencies are conditional:

### Camera

- ZED SDK for ZED hardware tests
- Intel RealSense SDK/tools for RealSense hardware tests
- ROS 2 Humble only for ROS-based camera tests

### LiDAR

- Livox SDK2 on the Jetson where the native Livox helper is required
- `livox_ros_driver2` for ROS-based LiDAR tests
- ROS 2 Humble for ROS integration

### Audio

- ALSA utilities (`aplay`, `arecord`, `amixer`)
- PulseAudio utilities (`pactl`)
- SoX and FFmpeg where a particular audio workflow needs them

### Bluetooth

- BlueZ tools such as `bluetoothctl` and `btmgmt`

### Wi-Fi

- `iw`
- NetworkManager/`nmcli`
- `iperf3`
- `ping` and standard IP networking tools

### Stress Test

- `stress-ng`
- `sysstat` tools such as `mpstat`
- Jetson-specific monitoring tools such as `tegrastats` when available on the DUT

Vendor SDKs, ROS distributions, CUDA, and Jetson-specific software must be installed separately according to the hardware vendor's instructions.

## Run Application

```bash
./scripts/run.sh
```

The script locates the repository, requires `.venv`, activates it, and runs `python -m desktop_app.main`. Run `setup_host.sh` or create the environment manually before using it.

## Run Tests

```bash
source .venv/bin/activate
python -m pytest -q
```

Some tests import Qt or inspect hardware-specific behavior. A test run on a machine without the relevant optional runtime, display support, hardware, or vendor SDK may skip tests or report environment-dependent failures.

## Environment Check

```bash
./scripts/check_environment.sh
```

The read-only check reports the host OS and architecture, required tools, virtualenv status, required Python imports, development tooling, and hardware command-line tools. Hardware and vendor checks are reported as `OPTIONAL` when missing and do not make the core host setup fail.

## Project Structure

Important directories in this repository include:

```text
desktop_app/   PySide6 application, UI pages, services, workers, and module logic
core/          Shared networking, remote access, Bluetooth, evidence, and test engine code
devices/       Camera, AI, and Livox device integrations
tests/         Unit and integration-style tests
testcases/     Camera, LiDAR, AI, Wi-Fi, and Stress Test definitions/data
scripts/       Application, validation, deployment, and utility scripts
evidence/      Repository evidence directory placeholder
docs/          Architecture and Stress Test documentation
jetson_tools/  Native Jetson helper source trees
```

## Troubleshooting

1. **`.venv` not found**

   Run `./scripts/setup_host.sh`, or create and activate the environment manually as shown above.

2. **`pytest` not found**

   ```bash
   source .venv/bin/activate
   python -m pip install -r requirements-dev.txt
   python -m pytest -q
   ```

3. **PySide6 or Qt xcb plugin errors**

   Re-run `./scripts/setup_host.sh` so the Ubuntu Qt/X11 runtime libraries are installed. Run the GUI from a local graphical session with a valid display; headless test execution may require the test's configured Qt environment.

4. **SSH `PermissionDenied`**

   Check the Jetson user, address, SSH service, firewall, key permissions, and the result of the BatchMode SSH command above. If using password authentication, confirm the application's password field is populated for that session.

5. **Missing vendor SDK**

   This is expected on a generic development host. Install the SDK on the host or Jetson only for the module that needs it, then run the environment check again.

6. **Hardware module unavailable**

   Confirm the device is connected, the required remote service is running, and the corresponding command-line tools are installed on the machine where the module executes.

7. **Application starts but hardware-specific functions are unavailable**

   Use the module's discovery/pre-test controls and inspect the application log. A successful GUI startup only confirms the core host environment; it does not prove that a camera, LiDAR, audio device, Bluetooth adapter, Wi-Fi interface, or vendor SDK is available.

## Evidence / Logs

- `evidence/` exists in the repository as a tracked placeholder; generated evidence should not be committed.
- Audio manual evidence defaults to `~/audio_test_evidence`.
- Stress Test evidence defaults to `~/Stress_Test_Logs` unless another directory is selected in the UI.
- Remote recording examples use paths below the Jetson user's home directory, such as `~/audio_test_logs/`; these files remain on the Jetson unless copied separately.

Do not commit generated evidence, local logs, credentials, or `.venv`.

## Git Workflow

- Work on a feature branch.
- Use `git fetch origin` before starting work that depends on recent remote changes.
- Inspect `git status` before and after edits.
- Run the relevant tests before committing.
- Commit tested changes and push the current branch only after review.
- Never commit secrets, generated evidence, local logs, or virtual environments.
