
# Hardware Test Automation Desktop App v0.1.1

PySide6 desktop application styled to match the previous Hardware Test Server UI.

## UI

- White topbar with Jetson connection pill
- Gray sidebar
- Dashboard
- Jetson Connection
- Camera Tests
- LiDAR Tests
- Test History
- Configuration
- Dark live-log / SSH console
- Teal accent `#168b91`

## Run

```bash
cd desktop_app
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

## SSH first-time verification

```bash
ssh huu@192.168.9.169
```

Accept the host fingerprint only after verifying it is the intended Jetson.

Then launch the application and use **Jetson Connection > CONNECT**.
