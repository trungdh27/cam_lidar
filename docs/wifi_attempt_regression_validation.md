# Wi-Fi attempt regression validation

STATUS: PARTIAL — implementation, focused regressions and offscreen visual checks completed; full hardware attempts/authentication through the running Dashboard session remain unverified.

Existing uncommitted changes were retained. No Wi-Fi rebuild, source-workbook rewrite, Dashboard connection change, commit or push was performed.

## TC-JET-STA-24G-002

DUT_WIFI_IP: 10.127.246.30 (current route/TCP probe target; runner discovers it afresh from the DUT interface).

CLIENT_WIFI_IF: wlx58044f6c4e0e.

ROUTE_DEV: wlx58044f6c4e0e — confirmed with the real laptop route.

TCP22: REACHABLE — real short socket probe completed.

SSH_VALIDATION_METHOD: SHARED SESSION when the actual peer IP matches and the local route qualifies; otherwise SHORT-LIVED DIRECT WIFI using the application's effective authentication settings. Both paths pass controlled tests. No live Dashboard session was available to this validation process.

SSH_AUTH: live NOT VALIDATED; controlled success PASS and authentication failure FAIL, with route/TCP still PASS.

REMOTE_COMMAND: live NOT VALIDATED; controlled WIFI_SSH_OK execution PASS; missing token produces REMOTE COMMAND FAILED.

EXPECTED_HOST: acquired from hostnamectl --static / hostname on the current shared session at runtime; controlled fixture current-jetson. No hard-coded production identity.

ACTUAL_HOST: live NOT VALIDATED; controlled fixture current-jetson.

HOST_IDENTITY: live NOT VALIDATED; controlled match PASS, mismatch FAIL.

RESULT: live NOT RUN; controlled full collector/evaluator PASS.

FAILURE_REASON: no DUT failure inferred. Safe, specific port/auth/host-key/config/command/identity reasons are logged and shown. Passwords and private-key contents are not captured.

## TC-JET-STA-24G-003

DISCONNECT_COUNT: 0 in controlled current-attempt collection; live full attempt NOT RUN.

DISCONNECT_EVENTS: [] in controlled clean attempts, including RUN AGAIN; timestamps/interface/state/source retained for real transitions.

CRITICAL: SSID/BSSID; frequency/band; ten numeric RSSI samples; PHY bitrate capture; no disconnect during the samples. The current authoritative ODS explicitly requires the last criterion, so it was not demoted.

SUPPORTING: timestamped disconnect evidence is expandable; optional BSSID/driver/PHY/Tx-power/width values remain informational. Sources without an explicit no-disconnect requirement get supporting disconnect telemetry instead.

RESULT: controlled full collector/evaluator PASS; live NOT RUN. Historical boot lines cannot qualify as attempt evidence. Missing/malformed or inconsistent event data produces MEASUREMENT ERROR, not DUT FAIL.

## TC-JET-STA-24G-005

AVG_RTT: 55.61 ms (provided historical observation, reused in controlled regression; not a new hardware measurement).

PACKET_LOSS: 0% (provided historical observation, reused in controlled regression).

DISCONNECT_COUNT: 0 in controlled current-attempt collection; live full attempt NOT RUN.

CRITICAL: Wi-Fi reachability; average RTT <=100 ms; source/project packet-loss acceptance; no disconnect, explicitly required by the current ODS.

SUPPORTING: expandable transition evidence and other diagnostic measurements. No maximum-RTT acceptance threshold was added.

RESULT: controlled full collector/evaluator PASS, with an explicit project packet-loss bound in the fixture. Existing qualitative acceptance/review behavior is preserved when no source/project numeric bound is configured. Live NOT RUN.

## CRITERIA TABLE

INITIAL FULL BUILD COUNT: 1 per controlled attempt (STA-002, STA-003, STA-005).

LIVE FULL REBUILD COUNT: 0, including timer/log updates, source FAIL styling and final PASS evaluation. Model reset/row-insert/row-remove signals remain zero after initial construction.

FLICKER FIX: PASS in offscreen regression. Stable item/widget identities, unchanged-cell no-ops, targeted Actual/Result updates, scroll/current-cell/selection preservation, and independent incremental event logs are verified. This is not a claim of a live hardware visual run.

Themed screenshots were rendered and inspected for all three TCs, including explicit SSH-auth failure with route/TCP still passing. Screenshots are in /tmp/wifi-attempt-regression/STA-*-pass.png and STA-002-auth-failure.png.

## Acceptance audit and safety

PYTEST: PASS — ./.venv/bin/python -m pytest -q: 500 passed, 53 collection warnings, 57.81 seconds. Thirty focused attempt regression cases are included.

VERIFY_SOURCE: PASS — ./scripts/verify_source.sh completed syntax, core/UI import and shell checks.

GIT DIFF CHECK: PASS — git diff --check.

All 42 AUTO cases are documented in wifi_attempt_criteria_audit.md with source acceptance and CRITICAL/SUPPORTING/INFORMATIONAL classification. Optional diagnostics do not become hard requirements. Endurance sources that forbid prolonged loss request review for transient events rather than inventing a zero-transient-loss limit.

CAMERA REGRESSION: no changes to Camera.

LIDAR REGRESSION: no changes to LiDAR.

SYSTEM STRESS REGRESSION: no changes to System Stress.

NO SECOND PERSISTENT SSH: CONFIRMED. Only a validation-owned temporary AsyncSSH connection can be created and is closed in finally; the shared manager/configuration is never replaced or modified.

NO COMMIT: CONFIRMED.

NO PUSH: CONFIRMED.

Full-suite validation initially exposed an existing discovery fixture calling real local tools and an intermittent process-stop timeout during concurrent runs. The discovery fixture now models unavailable local tools explicitly, and new synthetic UI fixtures disable real background discovery and dispose their widgets. No production Dashboard discovery behavior was changed for this test isolation.
