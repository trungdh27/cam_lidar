# AUTO Wi-Fi acceptance audit — attempt-local fixes

All 42 current ODS AUTO cases. Source acceptance is shown verbatim for traceability; metric availability is not an acceptance requirement.

The current source explicitly requires no disconnects in STA-003 and STA-005. Those checks remain critical. Disconnect telemetry is supporting when that requirement is absent. NetworkManager/driver/service and network-error diagnostics are supporting where the source does not explicitly require them.

STA-002 qualifies the fresh DUT Wi-Fi IP using the actual shared peer or a short-lived direct session with application authentication. Route, TCP, authentication, remote command and host identity are separate criteria. Collector inconsistencies are measurement errors, not DUT failures.

Endurance source clauses forbid prolonged loss, not every transient transition. With no source duration limit, nonzero transient events require review rather than an invented zero-disconnect DUT failure.

TC001 retains source channel/frequency text for traceability but evaluates generic AP runtime using valid-band channel and channel/frequency consistency. Exact RF values are critical only under an explicit per-band project lock. TC002 accepts either Wi-Fi 5/VHT/802.11ac or Wi-Fi 6/HE/802.11ax. TC003 discovers the target from Jetson AP runtime and evaluates association, client IPv4, DUT-specific route and interface-bound reachability independently before deriving the full qualification path.

## TC-JET-24G-001

Source: wifi_vd_tcs.ods

Source acceptance: AP ở type AP; đúng SSID; runtime ở 2.4 GHz; channel 6; frequency 2437 MHz; interface UP; IP AP hợp lệ; không lỗi regulatory/activation.

CRITICAL: AP mode → AP; Band → 2.4 GHz; Interface → UP; AP IPv4 → valid IPv4; Regulatory/activation errors → none

SUPPORTING: Channel → Valid 2.4 GHz channel; Frequency → Consistent with selected channel / band

INFORMATIONAL: SSID; BSSID; Driver; PHY; Tx power; Width; Interface; State; Address; RSSI

## TC-JET-24G-002

Source: wifi_vd_tcs.ods

Source acceptance: PASS Wi-Fi 6 khi có HE/802.11ax capability; nếu runtime tool expose PHY mode thì link có evidence HE. VHT-only không đủ chứng minh Wi-Fi 6.

CRITICAL: PHY capability → Wi-Fi 5 / 802.11ac / VHT or Wi-Fi 6 / 802.11ax / HE; Runtime PHY → Wi-Fi 5 / 802.11ac / VHT or Wi-Fi 6 / 802.11ax / HE

SUPPORTING: None

INFORMATIONAL: SSID; BSSID; Channel; RSSI

## TC-JET-24G-003

Source: wifi_vd_tcs.ods

Source acceptance: Client associated đúng BSSID/SSID và 2.4 GHz; có IPv4 hợp lệ; route tới 192.168.2.22 dùng client Wi-Fi interface, không đi qua Ethernet; ping thành công.

CRITICAL: Association → client associated with target Jetson AP SSID (runtime-discovered); Band → 2.4 GHz; Client IPv4 → valid IPv4; Wi-Fi route → route to DUT_AP_IP uses CLIENT_WIFI_IF; Reachability → DUT_AP_IP reachable through CLIENT_WIFI_IF

SUPPORTING: None

INFORMATIONAL: Interface; State; Address; RSSI

## TC-JET-24G-004

Source: wifi_vd_tcs.ods

Source acceptance: RSSI/PHY bitrate được ghi đầy đủ; link không disconnect; dữ liệu đủ để so sánh 2.4 GHz và 5 GHz.

CRITICAL: Disconnects during test → 0; Required measurement capture → Source-requested measurements recorded

SUPPORTING: None

INFORMATIONAL: RSSI; TX PHY bitrate; RX PHY bitrate; SSID; BSSID; Channel

## TC-JET-24G-005

Source: wifi_vd_tcs.ods

Source acceptance: Average RTT ≤100 ms; packet loss không bất thường; không disconnect. Max RTT được ghi để phân tích spike.

CRITICAL: Packets sent → collected; Packets received → collected; Average RTT → <= 100 ms; Packet loss → not abnormal (source is qualitative); Disconnects during test → 0; Required measurement capture → Source-requested measurements recorded

SUPPORTING: None

INFORMATIONAL: RTT min; RTT max; RTT mdev; RTT; Average RTT; Jitter

## TC-JET-24G-006

Source: wifi_vd_tcs.ods

Source acceptance: Receiver throughput mỗi chiều ≥5 Mbps cho baseline; không disconnect; Retr được ghi.

CRITICAL: Forward receiver throughput → >= 5 Mbps; Reverse receiver throughput → >= 5 Mbps; Disconnects during test → 0; Required measurement capture → Source-requested measurements recorded

SUPPORTING: None

INFORMATIONAL: Retransmits; RSSI; Upload; Download

## TC-JET-24G-007

Source: wifi_vd_tcs.ods

Source acceptance: UDP 5 Mbps duy trì gần target; loss thấp/0% ideal; jitter thấp. UDP 10 Mbps không làm mất link.

CRITICAL: UDP 5 Mbps receiver → near 5 Mbps target (source is qualitative); UDP loss → low / 0% ideal (source is qualitative); UDP jitter → low (source is qualitative); 10 Mbps link → link remains connected; UDP 5 Mbps run → completed; UDP 10 Mbps run → completed; Disconnects during test → none; Required measurement capture → Source-requested measurements recorded

SUPPORTING: Network errors → no Network is unreachable; NetworkManager errors → none

INFORMATIONAL: UDP 5 sender Mbps; UDP 5 lost datagrams; UDP 5 total datagrams; UDP 10 sender Mbps; UDP 10 lost datagrams; UDP 10 total datagrams; UDP 10 receiver Mbps; UDP 10 loss; UDP 10 jitter; RSSI; Upload; Download; Retransmits

## TC-JET-24G-008

Source: wifi_vd_tcs.ods

Source acceptance: Tại >1 m trong factory: SSID discoverable, client kết nối được, IP/route hợp lệ, ping/service usable, không disconnect bất thường.

CRITICAL: SSID discovery → visible at confirmed >1 m; Association → connected; IPv4/route → valid; Ping → succeeds; SSH → succeeds; Disconnects during test → 0; Physical distance → >=1 m confirmed

SUPPORTING: None

INFORMATIONAL: Interface; State; Address; RSSI

## TC-JET-24G-009

Source: wifi_vd_tcs.ods

Source acceptance: Tại 10 m, TCP receiver throughput ≥5 Mbps; không disconnect; forward/reverse đều được ghi.

CRITICAL: Forward receiver throughput → >= 5 Mbps at 10 m; Reverse receiver throughput → >= 5 Mbps at 10 m; Disconnects during test → 0; Physical distance → 10 m confirmed

SUPPORTING: None

INFORMATIONAL: RSSI; Upload; Download; Retransmits

## TC-JET-24G-010

Source: wifi_vd_tcs.ods

Source acceptance: WPA2 runtime được xác nhận; WPA3 PASS khi có SAE capability và runtime activation/client connection thành công; không open/WEP/WPA1-only.

CRITICAL: WPA2 runtime → enabled; SAE capability → available for WPA3; WPA3 activation → successful; Legacy/open security → none

SUPPORTING: None

INFORMATIONAL: Interface; State; Address; RSSI

## TC-JET-24G-011

Source: wifi_vd_tcs.ods

Source acceptance: Valid credential connect; invalid credential reject; DHCP cấp IP hợp lệ; 5/5 reconnect thành công; không duplicate IP/stale route.

CRITICAL: Valid credential → connects; Invalid credential → rejected; Temporary profile cleanup → deleted; Valid profile restoration → association, IPv4, route, reachability; DHCP → valid IPv4; Reconnect cycles → 5/5; Duplicate/stale IP → none; Phase A — Recovery cycle 1 → association, IPv4, Wi-Fi route and DUT reachability restored; Phase A — Recovery cycle 2 → association, IPv4, Wi-Fi route and DUT reachability restored; Phase A — Recovery cycle 3 → association, IPv4, Wi-Fi route and DUT reachability restored; Phase A — Recovery cycle 4 → association, IPv4, Wi-Fi route and DUT reachability restored; Phase A — Recovery cycle 5 → association, IPv4, Wi-Fi route and DUT reachability restored

SUPPORTING: None

INFORMATIONAL: Completed cycles; Successful cycles; Avg recovery; Failures

## TC-JET-24G-012

Source: wifi_vd_tcs.ods

Source acceptance: Client recovery 5/5; AP trở lại sau profile restart; DHCP/route/service hoạt động lại; không cần reboot.

CRITICAL: Phase A — Client recovery → 5/5; Phase B — AP recovery → restored; DHCP/route/service → restored; Reboot required → no; Phase A — Recovery cycle 1 → association, IPv4, Wi-Fi route and DUT reachability restored; Phase A — Recovery cycle 2 → association, IPv4, Wi-Fi route and DUT reachability restored; Phase A — Recovery cycle 3 → association, IPv4, Wi-Fi route and DUT reachability restored; Phase A — Recovery cycle 4 → association, IPv4, Wi-Fi route and DUT reachability restored; Phase A — Recovery cycle 5 → association, IPv4, Wi-Fi route and DUT reachability restored

SUPPORTING: None

INFORMATIONAL: Completed cycles; Successful cycles; Avg recovery; Failures

## TC-JET-24G-013

Source: wifi_vd_tcs.ods

Source acceptance: 2 giờ không mất SSID kéo dài, không reconnect bắt buộc/recovery thủ công; ping/service ổn định; không driver reset/NetworkManager failure.

CRITICAL: Endurance duration → >= 7200 s; Prolonged Wi-Fi loss → No prolonged loss; transient events require source review; Manual recovery → none; NetworkManager failures → none; Driver resets → none

SUPPORTING: Service failures → none

INFORMATIONAL: Elapsed; Min RSSI; Errors

## TC-JET-5G-001

Source: wifi_vd_tcs.ods

Source acceptance: AP ở type AP; đúng SSID; runtime ở 5 GHz; channel 36; frequency 5180 MHz; interface UP; IP AP hợp lệ; không lỗi regulatory/activation.

CRITICAL: AP mode → AP; Band → 5 GHz; Interface → UP; AP IPv4 → valid IPv4; Regulatory/activation errors → none

SUPPORTING: Channel → Valid 5 GHz channel; Frequency → Consistent with selected channel / band

INFORMATIONAL: SSID; BSSID; Driver; PHY; Tx power; Width; Interface; State; Address; RSSI

## TC-JET-5G-002

Source: wifi_vd_tcs.ods

Source acceptance: PASS Wi-Fi 6 khi có HE/802.11ax capability; nếu runtime tool expose PHY mode thì link có evidence HE. VHT-only không đủ chứng minh Wi-Fi 6.

CRITICAL: PHY capability → Wi-Fi 5 / 802.11ac / VHT or Wi-Fi 6 / 802.11ax / HE; Runtime PHY → Wi-Fi 5 / 802.11ac / VHT or Wi-Fi 6 / 802.11ax / HE

SUPPORTING: None

INFORMATIONAL: SSID; BSSID; Channel; RSSI

## TC-JET-5G-003

Source: wifi_vd_tcs.ods

Source acceptance: Client associated đúng BSSID/SSID và 5 GHz; có IPv4 hợp lệ; route tới 192.168.2.22 dùng client Wi-Fi interface, không đi qua Ethernet; ping thành công.

CRITICAL: Association → client associated with target Jetson AP SSID (runtime-discovered); Band → 5 GHz; Client IPv4 → valid IPv4; Wi-Fi route → route to DUT_AP_IP uses CLIENT_WIFI_IF; Reachability → DUT_AP_IP reachable through CLIENT_WIFI_IF

SUPPORTING: None

INFORMATIONAL: Interface; State; Address; RSSI

## TC-JET-5G-004

Source: wifi_vd_tcs.ods

Source acceptance: RSSI/PHY bitrate được ghi đầy đủ; link không disconnect; dữ liệu đủ để so sánh 2.4 GHz và 5 GHz.

CRITICAL: Disconnects during test → 0; Required measurement capture → Source-requested measurements recorded

SUPPORTING: None

INFORMATIONAL: RSSI; TX PHY bitrate; RX PHY bitrate; SSID; BSSID; Channel

## TC-JET-5G-005

Source: wifi_vd_tcs.ods

Source acceptance: Average RTT ≤100 ms; packet loss không bất thường; không disconnect. Max RTT được ghi để phân tích spike.

CRITICAL: Packets sent → collected; Packets received → collected; Average RTT → <= 100 ms; Packet loss → not abnormal (source is qualitative); Disconnects during test → 0; Required measurement capture → Source-requested measurements recorded

SUPPORTING: None

INFORMATIONAL: RTT min; RTT max; RTT mdev; RTT; Average RTT; Jitter

## TC-JET-5G-006

Source: wifi_vd_tcs.ods

Source acceptance: Receiver throughput mỗi chiều ≥5 Mbps cho baseline; không disconnect; Retr được ghi.

CRITICAL: Forward receiver throughput → >= 5 Mbps; Reverse receiver throughput → >= 5 Mbps; Disconnects during test → 0; Required measurement capture → Source-requested measurements recorded

SUPPORTING: None

INFORMATIONAL: Retransmits; RSSI; Upload; Download

## TC-JET-5G-007

Source: wifi_vd_tcs.ods

Source acceptance: UDP 5 Mbps duy trì gần target; loss thấp/0% ideal; jitter thấp. UDP 10 Mbps không làm mất link.

CRITICAL: UDP 5 Mbps receiver → near 5 Mbps target (source is qualitative); UDP loss → low / 0% ideal (source is qualitative); UDP jitter → low (source is qualitative); 10 Mbps link → link remains connected; UDP 5 Mbps run → completed; UDP 10 Mbps run → completed; Disconnects during test → none; Required measurement capture → Source-requested measurements recorded

SUPPORTING: Network errors → no Network is unreachable; NetworkManager errors → none

INFORMATIONAL: UDP 5 sender Mbps; UDP 5 lost datagrams; UDP 5 total datagrams; UDP 10 sender Mbps; UDP 10 lost datagrams; UDP 10 total datagrams; UDP 10 receiver Mbps; UDP 10 loss; UDP 10 jitter; RSSI; Upload; Download; Retransmits

## TC-JET-5G-008

Source: wifi_vd_tcs.ods

Source acceptance: Tại >1 m trong factory: SSID discoverable, client kết nối được, IP/route hợp lệ, ping/service usable, không disconnect bất thường.

CRITICAL: SSID discovery → visible at confirmed >1 m; Association → connected; IPv4/route → valid; Ping → succeeds; SSH → succeeds; Disconnects during test → 0; Physical distance → >=1 m confirmed

SUPPORTING: None

INFORMATIONAL: Interface; State; Address; RSSI

## TC-JET-5G-009

Source: wifi_vd_tcs.ods

Source acceptance: Tại 10 m, TCP receiver throughput ≥5 Mbps; không disconnect; forward/reverse đều được ghi.

CRITICAL: Forward receiver throughput → >= 5 Mbps at 10 m; Reverse receiver throughput → >= 5 Mbps at 10 m; Disconnects during test → 0; Physical distance → 10 m confirmed

SUPPORTING: None

INFORMATIONAL: RSSI; Upload; Download; Retransmits

## TC-JET-5G-010

Source: wifi_vd_tcs.ods

Source acceptance: WPA2 runtime được xác nhận; WPA3 PASS khi có SAE capability và runtime activation/client connection thành công; không open/WEP/WPA1-only.

CRITICAL: WPA2 runtime → enabled; SAE capability → available for WPA3; WPA3 activation → successful; Legacy/open security → none

SUPPORTING: None

INFORMATIONAL: Interface; State; Address; RSSI

## TC-JET-5G-011

Source: wifi_vd_tcs.ods

Source acceptance: Valid credential connect; invalid credential reject; DHCP cấp IP hợp lệ; 5/5 reconnect thành công; không duplicate IP/stale route.

CRITICAL: Valid credential → connects; Invalid credential → rejected; Temporary profile cleanup → deleted; Valid profile restoration → association, IPv4, route, reachability; DHCP → valid IPv4; Reconnect cycles → 5/5; Duplicate/stale IP → none; Phase A — Recovery cycle 1 → association, IPv4, Wi-Fi route and DUT reachability restored; Phase A — Recovery cycle 2 → association, IPv4, Wi-Fi route and DUT reachability restored; Phase A — Recovery cycle 3 → association, IPv4, Wi-Fi route and DUT reachability restored; Phase A — Recovery cycle 4 → association, IPv4, Wi-Fi route and DUT reachability restored; Phase A — Recovery cycle 5 → association, IPv4, Wi-Fi route and DUT reachability restored

SUPPORTING: None

INFORMATIONAL: Completed cycles; Successful cycles; Avg recovery; Failures

## TC-JET-5G-012

Source: wifi_vd_tcs.ods

Source acceptance: Client recovery 5/5; AP trở lại sau profile restart; DHCP/route/service hoạt động lại; không cần reboot.

CRITICAL: Phase A — Client recovery → 5/5; Phase B — AP recovery → restored; DHCP/route/service → restored; Reboot required → no; Phase A — Recovery cycle 1 → association, IPv4, Wi-Fi route and DUT reachability restored; Phase A — Recovery cycle 2 → association, IPv4, Wi-Fi route and DUT reachability restored; Phase A — Recovery cycle 3 → association, IPv4, Wi-Fi route and DUT reachability restored; Phase A — Recovery cycle 4 → association, IPv4, Wi-Fi route and DUT reachability restored; Phase A — Recovery cycle 5 → association, IPv4, Wi-Fi route and DUT reachability restored

SUPPORTING: None

INFORMATIONAL: Completed cycles; Successful cycles; Avg recovery; Failures

## TC-JET-5G-013

Source: wifi_vd_tcs.ods

Source acceptance: 2 giờ không mất SSID kéo dài, không reconnect bắt buộc/recovery thủ công; ping/service ổn định; không driver reset/NetworkManager failure.

CRITICAL: Endurance duration → >= 7200 s; Prolonged Wi-Fi loss → No prolonged loss; transient events require source review; Manual recovery → none; NetworkManager failures → none; Driver resets → none

SUPPORTING: Service failures → none

INFORMATIONAL: Elapsed; Min RSSI; Errors

## TC-JET-STA-24G-001

Source: wifi_vd_tcs.ods

Source acceptance: Jetson connected vào đúng SSID <TEST_WIFI_24G_SSID> trên 2.4 GHz; interface ở managed/station mode; nhận IPv4 + gateway hợp lệ; route qua Wi-Fi được thiết lập.

CRITICAL: Band → 2.4 GHz; Station mode → managed; IPv4 → valid; Gateway → valid; Wi-Fi route → present; Required measurement capture → Source-requested measurements recorded

SUPPORTING: None

INFORMATIONAL: SSID; Interface; State; Address; RSSI

## TC-JET-STA-24G-002

Source: wifi_vd_tcs.ods

Source acceptance: Ping tới Wi-Fi IP của Jetson thành công; SSH login thành công; route tới DUT dùng Wi-Fi client interface; session trả về đúng host Jetson.

CRITICAL: Ping → success through Wi-Fi; Wi-Fi route → uses client Wi-Fi interface; TCP port 22 → reachable (application SSH port); SSH authentication → login succeeds; Remote command execution → WIFI_SSH_OK returned; Host identity → expected Jetson host

SUPPORTING: None

INFORMATIONAL: Interface; State; Address; RSSI

## TC-JET-STA-24G-003

Source: wifi_vd_tcs.ods

Source acceptance: SSID/BSSID đúng external AP; frequency thuộc 2.4 GHz; RSSI và RX/TX PHY bitrate được ghi nhận; link không disconnect trong 10 mẫu.

CRITICAL: SSID/BSSID → external AP matches; Frequency → 2.4 GHz; RSSI samples → 10 samples; TX/RX bitrate → collected; Disconnects during test → 0

SUPPORTING: None

INFORMATIONAL: BSSID; Driver; PHY; Tx power; Width; SSID; Channel; RSSI

## TC-JET-STA-24G-004

Source: wifi_vd_tcs.ods

Source acceptance: Capability được ghi rõ. Wi-Fi 6 chỉ PASS khi có HE/802.11ax evidence; VHT-only chỉ chứng minh Wi-Fi 5.

CRITICAL: Capability → recorded; Wi-Fi 6 evidence → HE/802.11ax

SUPPORTING: None

INFORMATIONAL: SSID; BSSID; Channel; RSSI

## TC-JET-STA-24G-005

Source: wifi_vd_tcs.ods

Source acceptance: Client↔DUT reachable qua Wi-Fi; average RTT local ≤100 ms theo requirement; packet loss không bất thường; không disconnect.

CRITICAL: Client↔DUT reachability → reachable; Average RTT → <= 100 ms; Packet loss → not abnormal (source is qualitative); Disconnects during test → 0

SUPPORTING: None

INFORMATIONAL: RTT; Average RTT; Jitter

## TC-JET-STA-24G-006

Source: wifi_vd_tcs.ods

Source acceptance: TCP throughput được ghi hai chiều; nếu áp dụng requirement 10 m thì receiver ≥5 Mbps. UDP ghi được loss/jitter; không xảy ra Network is unreachable/disconnect.

CRITICAL: Forward receiver throughput → Recorded; 5 Mbps applies only to confirmed 10 m requirement; Reverse receiver throughput → Recorded; 5 Mbps applies only to confirmed 10 m requirement; Network errors → none; Disconnects during test → none; Required measurement capture → Source-requested measurements recorded

SUPPORTING: None

INFORMATIONAL: UDP loss; UDP jitter; RSSI; Upload; Download; Retransmits

## TC-JET-STA-24G-007

Source: wifi_vd_tcs.ods

Source acceptance: Runtime security được xác nhận; WPA2/WPA3 theo requirement; mạng không open/WEP/WPA1-only ngoài thiết kế; invalid credential bị reject, valid credential reconnect được.

CRITICAL: Runtime security → WPA2/WPA3; Insecure modes → none; Invalid credential → rejected; Valid reconnect → successful

SUPPORTING: None

INFORMATIONAL: Interface; State; Address; RSSI

## TC-JET-STA-24G-008

Source: wifi_vd_tcs.ods

Source acceptance: Không mất Wi-Fi kéo dài; SSH không tự drop bất thường; IP/route giữ hợp lệ; không có driver/NetworkManager reset; không cần recovery thủ công.

CRITICAL: Endurance duration → >= 1800 s (source minimum 30 minutes; 2 hours optional); Prolonged Wi-Fi loss → No prolonged loss; transient events require source review; SSH drops → none; IP/route → remains valid; Driver/NM resets → none; Manual recovery → none

SUPPORTING: None

INFORMATIONAL: Elapsed; Min RSSI; Errors

## TC-JET-STA-5G-001

Source: wifi_vd_tcs.ods

Source acceptance: Jetson connected vào đúng SSID <TEST_WIFI_5G_SSID> trên 5 GHz; interface ở managed/station mode; nhận IPv4 + gateway hợp lệ; route qua Wi-Fi được thiết lập.

CRITICAL: Band → 5 GHz; Station mode → managed; IPv4 → valid; Gateway → valid; Wi-Fi route → present; Required measurement capture → Source-requested measurements recorded

SUPPORTING: None

INFORMATIONAL: SSID; Interface; State; Address; RSSI

## TC-JET-STA-5G-002

Source: wifi_vd_tcs.ods

Source acceptance: Ping tới Wi-Fi IP của Jetson thành công; SSH login thành công; route tới DUT dùng Wi-Fi client interface; session trả về đúng host Jetson.

CRITICAL: Ping → success through Wi-Fi; Wi-Fi route → uses client Wi-Fi interface; TCP port 22 → reachable (application SSH port); SSH authentication → login succeeds; Remote command execution → WIFI_SSH_OK returned; Host identity → expected Jetson host

SUPPORTING: None

INFORMATIONAL: Interface; State; Address; RSSI

## TC-JET-STA-5G-003

Source: wifi_vd_tcs.ods

Source acceptance: SSID/BSSID đúng external AP; frequency thuộc 5 GHz; RSSI và RX/TX PHY bitrate được ghi nhận; link không disconnect trong 10 mẫu.

CRITICAL: SSID/BSSID → external AP matches; Frequency → 5 GHz; RSSI samples → 10 samples; TX/RX bitrate → collected; Disconnects during test → 0

SUPPORTING: None

INFORMATIONAL: BSSID; Driver; PHY; Tx power; Width; SSID; Channel; RSSI

## TC-JET-STA-5G-004

Source: wifi_vd_tcs.ods

Source acceptance: Capability được ghi rõ. Wi-Fi 6 chỉ PASS khi có HE/802.11ax evidence; VHT-only chỉ chứng minh Wi-Fi 5.

CRITICAL: Capability → recorded; Wi-Fi 6 evidence → HE/802.11ax

SUPPORTING: None

INFORMATIONAL: SSID; BSSID; Channel; RSSI

## TC-JET-STA-5G-005

Source: wifi_vd_tcs.ods

Source acceptance: Client↔DUT reachable qua Wi-Fi; average RTT local ≤100 ms theo requirement; packet loss không bất thường; không disconnect.

CRITICAL: Client↔DUT reachability → reachable; Average RTT → <= 100 ms; Packet loss → not abnormal (source is qualitative); Disconnects during test → 0

SUPPORTING: None

INFORMATIONAL: RTT; Average RTT; Jitter

## TC-JET-STA-5G-006

Source: wifi_vd_tcs.ods

Source acceptance: TCP throughput được ghi hai chiều; nếu áp dụng requirement 10 m thì receiver ≥5 Mbps. UDP ghi được loss/jitter; không xảy ra Network is unreachable/disconnect.

CRITICAL: Forward receiver throughput → Recorded; 5 Mbps applies only to confirmed 10 m requirement; Reverse receiver throughput → Recorded; 5 Mbps applies only to confirmed 10 m requirement; Network errors → none; Disconnects during test → none; Required measurement capture → Source-requested measurements recorded

SUPPORTING: None

INFORMATIONAL: UDP loss; UDP jitter; RSSI; Upload; Download; Retransmits

## TC-JET-STA-5G-007

Source: wifi_vd_tcs.ods

Source acceptance: Runtime security được xác nhận; WPA2/WPA3 theo requirement; mạng không open/WEP/WPA1-only ngoài thiết kế; invalid credential bị reject, valid credential reconnect được.

CRITICAL: Runtime security → WPA2/WPA3; Insecure modes → none; Invalid credential → rejected; Valid reconnect → successful

SUPPORTING: None

INFORMATIONAL: Interface; State; Address; RSSI

## TC-JET-STA-5G-008

Source: wifi_vd_tcs.ods

Source acceptance: Không mất Wi-Fi kéo dài; SSH không tự drop bất thường; IP/route giữ hợp lệ; không có driver/NetworkManager reset; không cần recovery thủ công.

CRITICAL: Endurance duration → >= 1800 s (source minimum 30 minutes; 2 hours optional); Prolonged Wi-Fi loss → No prolonged loss; transient events require source review; SSH drops → none; IP/route → remains valid; Driver/NM resets → none; Manual recovery → none

SUPPORTING: None

INFORMATIONAL: Elapsed; Min RSSI; Errors
