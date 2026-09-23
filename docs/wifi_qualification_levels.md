# AUTO Wi-Fi qualification levels

This runtime policy is derived from each test's measurement needs. It does not modify the source workbook. Target-specific `ip route get` evidence is collected freshly for every forward-path attempt.

| TC IDs | Qualification level | Reason |
|---|---|---|
| TC-JET-24G-001, TC-JET-5G-001 | `RUNTIME_WIFI_STATE` | Inspect AP mode, band and RF runtime state. |
| TC-JET-24G-002, TC-JET-5G-002 | `NONE` | PHY capability/standard does not require a client route. |
| TC-JET-24G-003, TC-JET-5G-003 | `FORWARD_WIFI_PATH` | Association, client IPv4, target route and reachability are independently evaluated. |
| TC-JET-24G-004, TC-JET-5G-004 | `RUNTIME_WIFI_STATE` | Inspect current RSSI and PHY bitrate. |
| TC-JET-24G-005…009, TC-JET-5G-005…009 | `FORWARD_WIFI_PATH` | RTT, throughput, discovery and range measurements target the DUT over client Wi-Fi. |
| TC-JET-24G-010, TC-JET-5G-010 | `RUNTIME_WIFI_STATE` | Inspect runtime security state. |
| TC-JET-24G-011…012, TC-JET-5G-011…012 | `CONTROL_PATH_RECOVERY` | Intentional disruption requires verified alternate management or tested automatic recovery. |
| TC-JET-24G-013, TC-JET-5G-013 | `FORWARD_WIFI_PATH` | Endurance reachability targets the DUT over client Wi-Fi. |
| TC-JET-STA-24G-001, TC-JET-STA-5G-001 | `RUNTIME_WIFI_STATE` | Inspect current DUT station association and addressing. |
| TC-JET-STA-24G-002, TC-JET-STA-5G-002 | `FORWARD_WIFI_PATH` | Laptop-to-DUT route, reachability and SSH use the target Wi-Fi IP. |
| TC-JET-STA-24G-003…004, TC-JET-STA-5G-003…004 | `RUNTIME_WIFI_STATE` | Inspect RF samples and PHY capability/runtime. |
| TC-JET-STA-24G-005…006, TC-JET-STA-5G-005…006 | `FORWARD_WIFI_PATH` | RTT and throughput target the DUT Wi-Fi IP. |
| TC-JET-STA-24G-007, TC-JET-STA-5G-007 | `CONTROL_PATH_RECOVERY` | Credential disruption needs a safe management/recovery strategy. |
| TC-JET-STA-24G-008, TC-JET-STA-5G-008 | `FORWARD_WIFI_PATH` | Endurance reachability targets the DUT over Wi-Fi. |

`BIDIRECTIONAL_WIFI_PATH` remains available in the shared qualifier for a future source requirement that explicitly requires independent reverse-route proof. Current AUTO cases do not promote optional reverse evidence to that level.
