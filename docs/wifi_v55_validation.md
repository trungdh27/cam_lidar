# Wi-Fi V5.5 port and validation

Reference read in full: `/home/test_laptop/Downloads/wifi_jetson_ap_sta_evidence_all_v5_5.sh` (1,389 lines).
The script was not executed or used as the application's runtime engine.

## Implemented behavior

- Read-only DUT/laptop discovery shares the existing Dashboard SSH architecture. DUT mode, band, interface, Wi-Fi IP, SSID, BSSID, gateway and active profile are discovered.
- Control classification distinguishes `WIFI_CONTROL`, `DUT_WIFI_DIRECT`, `ETHERNET_MGMT` and `OTHER_CONTROL`. Verified alternate management is independent of primary remote control.
- Wi-Fi qualification requires a connected laptop, matching SSIDs, a laptop route through its discovered Wi-Fi adapter, and the DUT return route through its Wi-Fi adapter. Static capability checks do not require this data path. Measurement collectors recheck the path before and after qualification and bind ping/iperf/SSH traffic to laptop Wi-Fi interface/address where supported by the existing commands.
- Matching saved laptop NetworkManager profiles provide valid access capability without reading their passwords. Discovery requests only SSID/profile/device metadata, never saved PSKs or `--show-secrets`.
- C11 preserves the valid profile, uses an isolated non-autoconnecting temporary invalid profile, verifies genuine authentication rejection, removes the temporary profile, restores association/IPv4/routes/reachability and evaluates each reconnect cycle separately. Cleanup/restoration runs on errors and cancellation; final restoration failure cannot pass.
- C12 has separate client-recovery and DUT AP-restart phases. AP restart requires AP mode, a known profile, explicit disruptive authorization and verified non-Wi-Fi management. Its UP command is attempted even if DOWN fails. Profile, SSID, band, DHCP, bidirectional path and reachability are checked after restart.
- The built-in client recovery also requires verified non-Wi-Fi management. No built-in local-only client recovery or detached Jetson AP self-recovery is advertised. A tested TC-specific workflow may be explicitly registered; merely setting a setup flag cannot enable reconnect execution.
- STA preparation is an explicit user action, never a discovery side effect. It requires verified management, prefers a saved profile, otherwise uses session-only user input, restores the previous profile if joining fails, and refreshes qualification afterwards. Already-established DUT STA association is inspected without redundant reconfiguration or a plaintext credential dependency.
- Existing iperf listeners are reused. App-owned listeners are cleaned up in `finally`, using only their captured PID/creation identity and dedicated temporary directory. No `pkill`, `killall` or user-listener termination is used.
- Test details expose exact blocking reasons, saved-profile access and C12 phase readiness. Results retain a non-secret runtime snapshot, per-cycle criteria and phase outcomes. Optional UDP project thresholds remain non-blocking.

## C11 / C12 environment inspection

Fresh read-only laptop checks were allowed outside the sandbox because sandboxed NetworkManager/netlink access was denied. No connections were changed.

The latest existing reference evidence is `~/wifi_test_logs/WIFI_AP_STA_V5_5_20260917_110929/`. Its DUT metadata shows **STA**, not AP, on `Minh Thai`. An attempted fresh non-interactive SSH probe to that recorded DUT endpoint, `agx@172.20.10.2`, returned `Permission denied (publickey,password)`. This diagnostic process does not have access to the running Dashboard's authenticated session. Therefore historical DUT metadata is not presented as a fresh live discovery or an observed status from that GUI.

| Requested field | Finding and provenance |
| --- | --- |
| AUTO-DETECTED DUT SSID | `Minh Thai`, latest V5.5 evidence; not freshly reverified |
| DUT mode / active profile | `managed` / `Minh Thai`, latest V5.5 evidence |
| LAPTOP WIFI INTERFACE | `wlx58044f6c4e0e`, freshly verified |
| LAPTOP ACTIVE PROFILE | `Minh Thai`, freshly verified |
| SAVED PROFILE AVAILABLE | YES: `Minh Thai` and `RD3.02`; their SSID metadata was freshly verified |
| PLAINTEXT CREDENTIAL READ | NO |
| Laptop → DUT via Wi-Fi | YES: route to `172.20.10.2` uses `wlx58044f6c4e0e`, source `172.20.10.4` |
| DUT → Laptop via Wi-Fi | Not freshly verified; latest reference route uses `wlP1p1s0` |
| FULL WIFI QUAL PATH | NO freshly proven full path; latest reference evidence had both Wi-Fi routes |
| PRIMARY CONTROL | Latest reference: Wi-Fi / `wlP1p1s0` / `172.20.10.2` (`DUT_WIFI_DIRECT`); fresh SSH authentication unavailable |
| ALTERNATE CONTROL | NO verified DUT management path in this validation; reference reports `MGMT_BACKUP_VALID=NO`. Laptop Ethernet presence is not DUT management proof. |
| C11 STATUS | BLOCKED for these diagnostic inputs: fresh authenticated control/AP runtime unavailable, and no verified non-Wi-Fi management or registered local recovery workflow. A saved profile is available; missing plaintext credentials are **not** the reason. |
| C12 PHASE A | BLOCKED for these diagnostic inputs: same missing client-recovery control strategy and unverified AP runtime. |
| C12 PHASE B | BLOCKED: no verified alternate management or implemented Jetson-side self-recovery; AP runtime/profile cannot be freshly qualified. |

These are conservative diagnostic readiness findings, not claims that the running Dashboard session is disconnected. Refresh from the application's authenticated shared connection obtains its real current status. Under the recorded STA state, AP C11/C12 correctly require AP mode rather than attempting an AP workflow on an infrastructure client.

## Automated and GUI validation

Focused tests exercise saved-profile access without PSKs, missing-profile setup, isolated wrong-password profiles, real rejection versus unrelated nmcli errors, successful and failed restore, 5/5 individual recovery cycles, cancellation cleanup, normal Wi-Fi SSH without Ethernet, AP restart gating, listener reuse/ownership cleanup, Ethernet-routed qualification rejection, and bidirectional Wi-Fi qualification success.

Headless Qt inspection at 1280×950: resizable PRE-TEST/setup splitter, setup scrollbar maximum `0`, compact naturally sized check table, and separate primary/backup control display. Source catalog counts remain AP 13/13 and STA 8/8; SSH-filtered STA visibility/reset behavior remains covered.

Validation commands:

```sh
QT_QPA_PLATFORM=offscreen ./.venv/bin/python -m pytest -q
./scripts/verify_source.sh
git diff --check
```

Final results: **410 tests passed** (53 collection warnings); all **79 focused Wi-Fi tests passed**; source verification and `git diff --check` passed. C11/C12 detail screenshots were inspected with saved-profile capability ready and alternate control unavailable; the two C12 phases and their explicit blockers remain distinct.

Physical C11/C12 disruption was not executed. No commit or push was performed. Camera, LiDAR, System Stress and Dashboard semantics were not modified by this port.
