# Wi-Fi tester UX / acceptance audit

Generated with `./.venv/bin/python scripts/wifi_criteria_audit.py`. Covers all 26 retained VD cases and 42 AUTO cases without rewriting either source workbook.

Critical checks determine acceptance. Supporting checks diagnose consistency. Informational values provide evidence without an invented threshold. When a source explicitly requires recording measurements, a critical capture check preserves that requirement while the measured values remain informational.

The shipped AUTO AP runtime source text is retained verbatim, including channel 6 / 2437 MHz and channel 36 / 5180 MHz examples. Because TC001 is a generic band/runtime validation, channel validity and channel/frequency consistency are SUPPORTING checks. They become CRITICAL exact values only when the matching per-band project RF lock is explicitly enabled. SSID is informational when no expected SSID is configured.

Wi-Fi 5 (VHT/802.11ac) and Wi-Fi 6 (HE/802.11ax) are both valid for the Wi-Fi 5/6 capability/runtime cases. Client association cases derive the target SSID and AP IPv4 from the running Jetson AP; missing target identity blocks preflight rather than rendering NOT CONFIGURED. Route, client IPv4 and interface-bound reachability remain independent criteria, and the full qualification path is derived after those checks.

Source corrections: STA TCP has a 5 Mbps bound only when the 10 m requirement applies; STA endurance has a 30-minute acceptance minimum and optional two-hour qualification. The existing two-hour collector schedule remains available. Average RTT keeps its 100 ms bound; maximum RTT, RSSI and retransmits gain no invented numeric acceptance limit. Qualitative packet loss / UDP acceptance continues to require review unless an explicit source/project limit resolves it.

Channel numbering and frequency consistency do not prove regulatory authorization. The evaluator uses regulatory-enabled PHY channels when exposed and preserves the separate source-required activation/regulatory-error check. [Linux regulatory documentation](https://docs.kernel.org/networking/regulatory.html).

Existing guided/manual cases retain inline acceptance review. Their source setup is documented below; this audit does not introduce new execution gates. Tools supporting automatic criteria must provide the corresponding measurement; optional tools/context do not turn into acceptance thresholds.

## TC-WIFI-C01 — Wi-Fi Hardware & Driver Bring-up

Source: `Jetson_WiFi_TCs_VD_completed.xlsx` · EXISTING

**Critical:** Wi-Fi interface exists → wlP1p1s0 exists; NetworkManager device type → TYPE=wifi; NetworkManager runtime state → state != unavailable; Kernel device detection → no relevant "Device not found" error; Driver detection → driver identified when exposed

**Supporting:** None

**Informational/context:** Firmware / hardware recognition; Hotspot profile

**Hard dependencies:** Shared Dashboard SSH for automatic capture; Configured Wi-Fi interface; journalctl -k -b; nmcli device status/show; nmcli/ip; sysfs/ethtool

**Optional dependencies:** ethtool -i

**Source setup (tester verification):** Robot đã boot hoàn tất; có terminal local hoặc SSH qua Ethernet.

## TC-WIFI-C02 — Radio / AP Capability Audit

Source: `Jetson_WiFi_TCs_VD_completed.xlsx` · EXISTING

**Critical:** AP mode capability → AP supported; Required hardware bands → 2.4 GHz and 5 GHz; AP+STA concurrency → confirm if production requires concurrency

**Supporting:** None

**Informational/context:** Interface; Driver/PHY; NetworkManager; Hotspot profile

**Hard dependencies:** Tester confirmation of source setup / acceptance; iw list

**Optional dependencies:** Shared SSH read-only snapshot where implemented; Original output import for guided/manual measurement

**Source setup (tester verification):** TC-WIFI-C01 PASS; driver Wi-Fi đã load.

## TC-WIFI-C03 — NetworkManager & Wi-Fi Baseline

Source: `Jetson_WiFi_TCs_VD_completed.xlsx` · EXISTING

**Critical:** NetworkManager service → active; Hotspot profile → Hotspot present; Wi-Fi interface visible to iw → wlP1p1s0 visible; Runtime Wi-Fi role → managed or AP

**Supporting:** None

**Informational/context:** Driver/PHY

**Hard dependencies:** Shared Dashboard SSH for automatic capture; Configured Wi-Fi interface; iw dev; nmcli connection show; systemctl is-active NetworkManager

**Optional dependencies:** None

**Source setup (tester verification):** Robot đã boot; TC-WIFI-C01 PASS.

## TC-WIFI-C04 — Hotspot Profile Configuration Audit

Source: `Jetson_WiFi_TCs_VD_completed.xlsx` · EXISTING

**Critical:** Hotspot profile exists → Hotspot present; AP profile mode → mode=ap; Production SSID → NOT CONFIGURED; Security configuration → WPA2/WPA3 per project; IPv4 sharing → NOT CONFIGURED; Autoconnect and permissions → NOT CONFIGURED; Band and channel → NOT CONFIGURED

**Supporting:** None

**Informational/context:** Interface; Driver/PHY; NetworkManager

**Hard dependencies:** Tester confirmation of source setup / acceptance; nmcli connection show

**Optional dependencies:** Shared SSH read-only snapshot where implemented; Original output import for guided/manual measurement

**Source setup (tester verification):** Profile `Hotspot` tồn tại; chưa cần activate AP.

## TC-WIFI-C05 — AP Activation, Runtime Mode & SSID Broadcast

Source: `Jetson_WiFi_TCs_VD_completed.xlsx` · EXISTING

**Critical:** Hotspot activation → `Hotspot` active trên `wlP1p1s0`; Runtime AP mode → `iw dev` hiển thị `type AP`; Profile SSID → SSID đúng profile; Client SSID discovery → client bên ngoài nhìn thấy SSID trong scan list

**Supporting:** None

**Informational/context:** AP state; SSID; Band; Activation elapsed

**Hard dependencies:** Shared Dashboard SSH for automatic capture; Configured Wi-Fi interface

**Optional dependencies:** None

**Source setup (tester verification):** Có truy cập Jetson qua Ethernet/console dự phòng; TC-WIFI-C04 PASS. Lưu ý việc activate Hotspot có thể ngắt kết nối Wi-Fi `Mr HIP` hiện tại.

## TC-WIFI-C06 — Valid Client Connection: Auth → DHCP → Ping → SSH

Source: `Jetson_WiFi_TCs_VD_completed.xlsx` · EXISTING

**Critical:** Authentication → Authentication thành công; Client IPv4 / gateway → client nhận IPv4/gateway hợp lệ; AP subnet → Jetson có AP IPv4 đúng subnet; Packet loss → ping đạt packet-loss requirement; SSH host identity → SSH trả về hostname `RD03-1425025055966`

**Supporting:** None

**Informational/context:** Authentication state; Assigned IPv4; DHCP state; Connection elapsed

**Hard dependencies:** Shared Dashboard SSH for automatic capture; Configured Wi-Fi interface

**Optional dependencies:** None

**Source setup (tester verification):** TC-WIFI-C05 PASS; biết `<ROBOT_SSID>` và credential hợp lệ.

## TC-WIFI-C07 — Invalid / Missing Wi-Fi Credential Rejection

Source: `Jetson_WiFi_TCs_VD_completed.xlsx` · EXISTING

**Critical:** Invalid / missing credential rejection → Client không join AP bằng missing/invalid credential và không được cấp IP từ subnet robot

**Supporting:** None

**Informational/context:** Attempts; Successful; Rejected; Auth time

**Hard dependencies:** Shared Dashboard SSH for automatic capture; Configured Wi-Fi interface

**Optional dependencies:** None

**Source setup (tester verification):** SSID đang broadcast; client có thể xóa/forget profile cũ.

## TC-WIFI-C09 — Security Exposure Audit

Source: `Jetson_WiFi_TCs_VD_completed.xlsx` · EXISTING

**Critical:** Secret exposure → Không có plaintext PSK trong output thông thường; Allowed services → chỉ các service được phép lắng nghe; Invalid SSH credential rejection → SSH credential sai không tạo được shell session (hoặc N/A nếu key-only)

**Supporting:** None

**Informational/context:** Security; Secret exposure; Service exposure; Warnings

**Hard dependencies:** Shared Dashboard SSH for automatic capture; Configured Wi-Fi interface

**Optional dependencies:** None

**Source setup (tester verification):** Hotspot active; client kết nối được; có service policy/allowlist nếu dự án quy định.

## TC-WIFI-C10 — Long Ping Stability

Source: `Jetson_WiFi_TCs_VD_completed.xlsx` · EXISTING

**Critical:** Ping completion → 1000 packets transmitted; Latency and link stability → source/project threshold and no disconnect

**Supporting:** None

**Informational/context:** RTT; Average RTT; Jitter; Packet loss

**Hard dependencies:** Shared Dashboard SSH for automatic capture; Configured Wi-Fi interface

**Optional dependencies:** None

**Source setup (tester verification):** Client kết nối ổn định; robot/client cố định tại vị trí test; không chạy iperf3 song song.

## TC-WIFI-C11 — RF Runtime Quality

Source: `Jetson_WiFi_TCs_VD_completed.xlsx` · EXISTING

**Critical:** Configured channel / width / power → Channel/bandwidth/txpower đúng cấu hình; RF stability / configured limits → signal và bitrate ổn định, đạt ngưỡng RF tại vị trí test

**Supporting:** None

**Informational/context:** SSID; BSSID; Channel; RSSI

**Hard dependencies:** Shared Dashboard SSH for automatic capture; Configured Wi-Fi interface

**Optional dependencies:** None

**Source setup (tester verification):** Hotspot active; ít nhất 1 client connected; ghi khoảng cách/vật cản.

## TC-WIFI-C12 — TCP Throughput & Concurrent Management Responsiveness

Source: `Jetson_WiFi_TCs_VD_completed.xlsx` · EXISTING

**Critical:** Throughput measured → upload and download measured; Throughput and stability → project threshold, no disconnect, ping/SSH responsive

**Supporting:** None

**Informational/context:** RSSI; Upload; Download; Retransmits

**Hard dependencies:** Shared Dashboard SSH for automatic capture; Configured Wi-Fi interface

**Optional dependencies:** None

**Source setup (tester verification):** Cài `iperf3` trên Jetson/laptop; client kết nối AP; SSH hoạt động.

## TC-WIFI-C13 — Client Wi-Fi Off/On Reconnection

Source: `Jetson_WiFi_TCs_VD_completed.xlsx` · EXISTING

**Critical:** Reconnect cycles → all recorded cycles pass; IP, ping and recovery time → source requirement met

**Supporting:** None

**Informational/context:** Completed cycles; Successful cycles; Avg recovery; Failures

**Hard dependencies:** Shared Dashboard SSH for automatic capture; Configured Wi-Fi interface

**Optional dependencies:** None

**Source setup (tester verification):** Client đã kết nối thành công và lưu profile SSID robot.

## TC-WIFI-C14 — AP Profile & NetworkManager Recovery

Source: `Jetson_WiFi_TCs_VD_completed.xlsx` · EXISTING

**Critical:** AP restoration → Sau mỗi failure injection, AP trở lại trạng thái production; Client recovery / ping → client reconnect/ping được; Ethernet preservation → `eno1`/`eth1` vẫn theo trạng thái thiết kế; Reboot-free recovery → không cần reboot robot

**Supporting:** None

**Informational/context:** Recovery step; AP state; Client recovered; Recovery elapsed

**Hard dependencies:** Shared Dashboard SSH for automatic capture; Configured Wi-Fi interface

**Optional dependencies:** None

**Source setup (tester verification):** Có Ethernet/console dự phòng; client sẵn sàng reconnect; production Ethernet đang up.

## TC-WIFI-C15 — Reboot Autostart & Boot-to-Wi-Fi-Ready — 3 Cycles

Source: `Jetson_WiFi_TCs_VD_completed.xlsx` · EXISTING

**Critical:** Three AP autostart cycles → 3/3 cycle AP tự phát SSID; Automatic DHCP / ping → client nhận IP và ping được mà không can thiệp thủ công; Configured boot-ready time → T_ready từng cycle đạt requirement

**Supporting:** None

**Informational/context:** Completed cycles; Average ready time; Max ready time; Failures

**Hard dependencies:** Shared Dashboard SSH for automatic capture; Configured Wi-Fi interface

**Optional dependencies:** None

**Source setup (tester verification):** Hotspot được cấu hình autoconnect; có laptop scan Wi-Fi; có phương án truy cập robot sau reboot.

## TC-WIFI-C16 — 2-Hour AP Endurance with 30-Min Checkpoint

Source: `Jetson_WiFi_TCs_VD_completed.xlsx` · EXISTING

**Critical:** Endurance duration → 2 hours completed; Link stability → no prolonged disconnect or manual intervention

**Supporting:** None

**Informational/context:** Elapsed; Disconnects; Min RSSI; Errors

**Hard dependencies:** Shared Dashboard SSH for automatic capture; Configured Wi-Fi interface

**Optional dependencies:** None

**Source setup (tester verification):** Pin/nguồn đủ ≥2 giờ; client cố định; production services/workload chạy bình thường.

## TC-WIFI-C17 — Ethernet + Wi-Fi AP Coexistence & Route Preservation

Source: `Jetson_WiFi_TCs_VD_completed.xlsx` · EXISTING

**Critical:** AP operation → `wlP1p1s0` hoạt động AP; Ethernet preservation → `eno1`/`eth1` giữ trạng thái production; Production route preservation → route quan trọng vẫn qua đúng interface và default route không bị thay đổi ngoài thiết kế

**Supporting:** None

**Informational/context:** Wi-Fi AP; Ethernet route; NetworkManager; Errors

**Hard dependencies:** Shared Dashboard SSH for automatic capture; Configured Wi-Fi interface

**Optional dependencies:** None

**Source setup (tester verification):** Có Ethernet/console dự phòng; biết trạng thái expected của `eno1` và `eth1`.

## TC-WIFI-C18 — Production Network & Robot Service Reachability While AP Active

Source: `Jetson_WiFi_TCs_VD_completed.xlsx` · EXISTING

**Critical:** Production Ethernet reachability → Production endpoint reachable qua đúng Ethernet; Wi-Fi SSH → client Wi-Fi SSH được vào Jetson; Production service policy → production ROS2/service phản hồi theo policy

**Supporting:** None

**Informational/context:** Wi-Fi AP; Ethernet route; NetworkManager; Errors

**Hard dependencies:** Shared Dashboard SSH for automatic capture; Configured Wi-Fi interface

**Optional dependencies:** None

**Source setup (tester verification):** Hotspot active; biết `<PRODUCTION_DEVICE_IP>`; client connected; ROS2/service production đang chạy.

## TC-WIFI-C19 — Wi-Fi Resource & Error Log Health

Source: `Jetson_WiFi_TCs_VD_completed.xlsx` · EXISTING

**Critical:** Resource health → Không có CPU/RAM spike kéo dài ngoài baseline; Disruptive / repeated errors → không có lỗi nghiêm trọng/lặp lại gây gián đoạn AP hoặc production network

**Supporting:** None

**Informational/context:** Wi-Fi AP; Ethernet route; NetworkManager; Errors

**Hard dependencies:** Shared Dashboard SSH for automatic capture; Configured Wi-Fi interface

**Optional dependencies:** None

**Source setup (tester verification):** Hotspot active; ít nhất 1 client connected; đã chạy các bài load/reliability chính.

## TC-WIFI-C20 — Full Wi-Fi Production End-to-End Acceptance

Source: `Jetson_WiFi_TCs_VD_completed.xlsx` · EXISTING

**Critical:** Automatic production readiness → Robot đạt trạng thái Wi-Fi production-ready end-to-end mà không thao tác thủ công; Client usability → client sử dụng được Wi-Fi; Production network / services → production network/service không bị ảnh hưởng; Serious errors → không có lỗi nghiêm trọng

**Supporting:** None

**Informational/context:** Wi-Fi AP; Ethernet route; NetworkManager; Errors

**Hard dependencies:** Shared Dashboard SSH for automatic capture; Configured Wi-Fi interface

**Optional dependencies:** None

**Source setup (tester verification):** Tất cả TC P0 bắt buộc đã PASS; không còn defect blocker.

## TC-WIFI-C21 — 2.4 GHz AP Operation & Client Connectivity

Source: `Jetson_WiFi_TCs_VD_completed.xlsx` · EXISTING

**Critical:** Hotspot activation → `Hotspot` active trên `wlP1p1s0`; AP mode → runtime `type AP`; Band → 2.4 GHz; SSID discovery → SSID `RD3.02` visible; Authentication / DHCP → client authentication thành công, nhận IPv4/gateway hợp lệ; Ping / SSH → ping và SSH Jetson thành công; Station evidence / stability → station dump thấy client và link ổn định; Unexpected disconnects → không có disconnect bất thường

**Supporting:** None

**Informational/context:** Band; Channel; Frequency; RSSI

**Hard dependencies:** Shared Dashboard SSH for automatic capture; Configured Wi-Fi interface

**Optional dependencies:** None

**Source setup (tester verification):** TC-WIFI-C02 PASS; profile `Hotspot` tồn tại; laptop hỗ trợ 2.4 GHz; có Ethernet/console dự phòng vì việc đổi band có thể làm mất phiên Wi-Fi hiện tại.

## TC-WIFI-C22 — 5 GHz AP Operation & Client Connectivity

Source: `Jetson_WiFi_TCs_VD_completed.xlsx` · EXISTING

**Critical:** AP activation → `Hotspot` active ở `type AP`; Band → 5 GHz; SSID discovery → SSID `RD3.02` visible; Authentication / DHCP → client join thành công, nhận IPv4/gateway hợp lệ; Ping / SSH → ping/SSH Jetson thành công; Station evidence → station dump hợp lệ; Regulatory errors / disconnects → không có lỗi channel/regulatory hoặc disconnect bất thường

**Supporting:** None

**Informational/context:** Band; Channel; Frequency; RSSI

**Hard dependencies:** Shared Dashboard SSH for automatic capture; Configured Wi-Fi interface

**Optional dependencies:** None

**Source setup (tester verification):** TC-WIFI-C02 PASS; profile `Hotspot` tồn tại; laptop hỗ trợ 5 GHz; regulatory domain/channel 5 GHz hợp lệ đã được xác định; có Ethernet/console dự phòng.

## TC-WIFI-C23 — 2.4 GHz vs 5 GHz Throughput @10 m

Source: `Jetson_WiFi_TCs_VD_completed.xlsx` · EXISTING

**Critical:** Physical distance → 10 m confirmed; 2.4 GHz throughput → >= 5 Mbps over 3 runs; 5 GHz throughput → >= 5 Mbps over 3 runs; Connection stability → no disconnect or abnormal retransmits

**Supporting:** None

**Informational/context:** Requirement; Retransmits

**Hard dependencies:** Shared Dashboard SSH for automatic capture; Configured Wi-Fi interface

**Optional dependencies:** None

**Source setup (tester verification):** TC-WIFI-C21 và TC-WIFI-C22 PASS; `iperf3` có trên Jetson/laptop; vị trí đo được xác định 10 m; robot/client cố định; hạn chế thay đổi vật cản và workload giữa hai lần đo.

## TC-WIFI-C24 — Wi-Fi 6 (802.11ax) Runtime Association Validation

Source: `Jetson_WiFi_TCs_VD_completed.xlsx` · EXISTING

**Critical:** Wi-Fi 6 capability / runtime HE → Jetson/client đều hỗ trợ 802.11ax và phiên liên kết runtime có evidence HE/802.11ax (ví dụ HE-MCS/HE-NSS hoặc PHY mode tương đương); Link stability / fallback → connection ổn định và không fallback cố định sang 802.11n/ac ngoài thiết kế

**Supporting:** None

**Informational/context:** Jetson HE; Client HE; Runtime mode; HE-MCS

**Hard dependencies:** Shared Dashboard SSH for automatic capture; Configured Wi-Fi interface

**Optional dependencies:** None

**Source setup (tester verification):** TC-WIFI-C02 PASS; client Wi-Fi 6/802.11ax; AP active; driver/client tools có thể hiển thị HE/PHY information; signal đủ tốt để tránh fallback do RF quá yếu.

## TC-WIFI-C25 — Factory Building Minimum Range Validation

Source: `Jetson_WiFi_TCs_VD_completed.xlsx` · EXISTING

**Critical:** Factory discovery at >=1 m → Tại khoảng cách ≥1 m trong factory building, SSID vẫn visible; Client association / IPv4 → client kết nối/nhận IP bình thường; Ping / SSH → ping và SSH hoạt động; Prolonged disconnects → không có disconnect kéo dài; Local RF usability → link RF đủ ổn định để đáp ứng sử dụng local

**Supporting:** None

**Informational/context:** Distance; SSID visible; RSSI; Disconnects

**Hard dependencies:** Shared Dashboard SSH for automatic capture; Configured Wi-Fi interface

**Optional dependencies:** None

**Source setup (tester verification):** TC-WIFI-C21 hoặc C22 PASS; xác định khu vực factory test; robot/client hoạt động ổn định; không thay đổi AP configuration trong run.

## TC-WIFI-C26 — Local Wi-Fi RTT Latency Validation

Source: `Jetson_WiFi_TCs_VD_completed.xlsx` · EXISTING

**Critical:** Ping completion → 100 packets transmitted; Average local RTT → <= 100 ms; Timeouts and packet loss → source/project limits met

**Supporting:** None

**Informational/context:** RTT; Jitter; Packet loss

**Hard dependencies:** Shared Dashboard SSH for automatic capture; Configured Wi-Fi interface

**Optional dependencies:** None

**Source setup (tester verification):** Client connected vào `RD3.02`; không chạy heavy throughput test đồng thời trừ khi requirement yêu cầu; vị trí và band test được ghi rõ.

## TC-WIFI-C27 — WPA2 / WPA3 Security Validation

Source: `Jetson_WiFi_TCs_VD_completed.xlsx` · EXISTING

**Critical:** Authentication evidence → valid/invalid credential behavior confirmed; Production security restored → configuration restored; WPA2 operation → valid WPA2 client can use the network; Required WPA3 / SAE operation → WPA3 works when mandatory; otherwise confirm not required; Rejected client IPv4 → invalid credential receives no robot-subnet IP; Secret exposure → no plaintext PSK in evidence

**Supporting:** None

**Informational/context:** WPA2; WPA3; Key management; Production restored

**Hard dependencies:** Shared Dashboard SSH for automatic capture; Configured Wi-Fi interface

**Optional dependencies:** None

**Source setup (tester verification):** Jetson/client hỗ trợ security mode cần test; có Ethernet/console dự phòng; lưu cấu hình production hiện tại trước khi đổi key management; credential test hợp lệ được chuẩn bị.

## TC-JET-24G-001 — AP runtime interface / SSID / mode / band validation

Source: `wifi_vd_tcs.ods` · AUTO

**Critical:** AP mode → AP; Band → 2.4 GHz; Interface → UP; AP IPv4 → valid IPv4; Regulatory/activation errors → none

**Supporting:** Channel → Valid 2.4 GHz channel; Frequency → Consistent with selected channel / band

**Informational/context:** SSID; BSSID; Driver; PHY; Tx power; Width; Interface; State; Address; RSSI

**Hard dependencies:** Shared SSH; Control interface; Wi-Fi interface; nmcli; NetworkManager; iw; Current Wi-Fi mode; Current SSID; Current band

**Optional dependencies:** AP profile; Wi-Fi PHY; Target band capability; Valid network path; Full Wi-Fi qualification path; Required Wi-Fi association; Laptop Wi-Fi interface; Jetson AP IP; iperf3 local; iperf3 Jetson; Backup Ethernet; Auto reconnect; Self-disruption safety; Position confirmed >=1 m; Recorded >=1 m distance; Position confirmed 10 m; Recorded 10 m distance; Valid Wi-Fi credential; External 2.4G STA SSID; External STA credential; Max packet loss (%); Max UDP loss (%); Max UDP jitter (ms); Min UDP receiver (Mbps); Security requirement

**Source setup (tester verification):** Jetson boot ổn định; có SSH Ethernet dự phòng; AP profile được xác nhận; client test có Wi-Fi và iperf3; thay đổi band/channel/security chỉ theo procedure được phê duyệt; restore production config sau test.

## TC-JET-24G-002 — Wi-Fi 5/6 capability and runtime PHY validation

Source: `wifi_vd_tcs.ods` · AUTO

**Critical:** PHY capability → Wi-Fi 5 / 802.11ac / VHT or Wi-Fi 6 / 802.11ax / HE; Runtime PHY → Wi-Fi 5 / 802.11ac / VHT or Wi-Fi 6 / 802.11ax / HE

**Supporting:** None

**Informational/context:** SSID; BSSID; Channel; RSSI

**Hard dependencies:** Shared SSH; Control interface; Wi-Fi interface; iw; Wi-Fi PHY; Target band capability

**Optional dependencies:** AP profile; nmcli; NetworkManager; Current Wi-Fi mode; Current SSID; Current band; Valid network path; Full Wi-Fi qualification path; Required Wi-Fi association; Laptop Wi-Fi interface; Jetson AP IP; iperf3 local; iperf3 Jetson; Backup Ethernet; Auto reconnect; Self-disruption safety; Position confirmed >=1 m; Recorded >=1 m distance; Position confirmed 10 m; Recorded 10 m distance; Valid Wi-Fi credential; External 2.4G STA SSID; External STA credential; Max packet loss (%); Max UDP loss (%); Max UDP jitter (ms); Min UDP receiver (Mbps); Security requirement

**Source setup (tester verification):** Jetson boot ổn định; có SSH Ethernet dự phòng; AP profile được xác nhận; client test có Wi-Fi và iperf3; thay đổi band/channel/security chỉ theo procedure được phê duyệt; restore production config sau test.

## TC-JET-24G-003 — Client association, IPv4 and Wi-Fi route validation

Source: `wifi_vd_tcs.ods` · AUTO

**Critical:** Association → client associated with target Jetson AP SSID (runtime-discovered); Band → 2.4 GHz; Client IPv4 → valid IPv4; Wi-Fi route → route to DUT_AP_IP uses CLIENT_WIFI_IF; Reachability → DUT_AP_IP reachable through CLIENT_WIFI_IF

**Supporting:** None

**Informational/context:** Interface; State; Address; RSSI

**Hard dependencies:** Shared SSH; Control interface; Wi-Fi interface; nmcli; NetworkManager; iw; Current SSID; Current band; Laptop Wi-Fi interface; Jetson AP IP

**Optional dependencies:** AP profile; Wi-Fi PHY; Target band capability; Current Wi-Fi mode; iperf3 local; iperf3 Jetson; Backup Ethernet; Auto reconnect; Self-disruption safety; Position confirmed >=1 m; Recorded >=1 m distance; Position confirmed 10 m; Recorded 10 m distance; Valid Wi-Fi credential; External 2.4G STA SSID; External STA credential; Max packet loss (%); Max UDP loss (%); Max UDP jitter (ms); Min UDP receiver (Mbps); Security requirement

**Source setup (tester verification):** Jetson boot ổn định; có SSH Ethernet dự phòng; AP profile được xác nhận; client test có Wi-Fi và iperf3; thay đổi band/channel/security chỉ theo procedure được phê duyệt; restore production config sau test.

## TC-JET-24G-004 — RSSI and PHY bitrate validation

Source: `wifi_vd_tcs.ods` · AUTO

**Critical:** Disconnects → 0; Required measurement capture → Source-requested measurements recorded

**Supporting:** None

**Informational/context:** RSSI; TX PHY bitrate; RX PHY bitrate; SSID; BSSID; Channel

**Hard dependencies:** Shared SSH; Control interface; Wi-Fi interface; iw; Wi-Fi PHY; Target band capability; Current band; Required Wi-Fi association; Laptop Wi-Fi interface

**Optional dependencies:** AP profile; nmcli; NetworkManager; Current Wi-Fi mode; Current SSID; Valid network path; Full Wi-Fi qualification path; Jetson AP IP; iperf3 local; iperf3 Jetson; Backup Ethernet; Auto reconnect; Self-disruption safety; Position confirmed >=1 m; Recorded >=1 m distance; Position confirmed 10 m; Recorded 10 m distance; Valid Wi-Fi credential; External 2.4G STA SSID; External STA credential; Max packet loss (%); Max UDP loss (%); Max UDP jitter (ms); Min UDP receiver (Mbps); Security requirement

**Source setup (tester verification):** Jetson boot ổn định; có SSH Ethernet dự phòng; AP profile được xác nhận; client test có Wi-Fi và iperf3; thay đổi band/channel/security chỉ theo procedure được phê duyệt; restore production config sau test.

## TC-JET-24G-005 — Local RTT latency and packet-loss validation

Source: `wifi_vd_tcs.ods` · AUTO

**Critical:** Packets sent → collected; Packets received → collected; Average RTT → <= 100 ms; Packet loss → not abnormal (source is qualitative); Disconnects → 0; Required measurement capture → Source-requested measurements recorded

**Supporting:** None

**Informational/context:** RTT min; RTT max; RTT mdev; RTT; Average RTT; Jitter

**Hard dependencies:** Shared SSH; Control interface; Wi-Fi interface; nmcli; NetworkManager; iw; Current band; Valid network path; Full Wi-Fi qualification path; Required Wi-Fi association; Laptop Wi-Fi interface; Jetson AP IP

**Optional dependencies:** AP profile; Wi-Fi PHY; Target band capability; Current Wi-Fi mode; Current SSID; iperf3 local; iperf3 Jetson; Backup Ethernet; Auto reconnect; Self-disruption safety; Position confirmed >=1 m; Recorded >=1 m distance; Position confirmed 10 m; Recorded 10 m distance; Valid Wi-Fi credential; External 2.4G STA SSID; External STA credential; Max packet loss (%); Max UDP loss (%); Max UDP jitter (ms); Min UDP receiver (Mbps); Security requirement

**Source setup (tester verification):** Jetson boot ổn định; có SSH Ethernet dự phòng; AP profile được xác nhận; client test có Wi-Fi và iperf3; thay đổi band/channel/security chỉ theo procedure được phê duyệt; restore production config sau test.

## TC-JET-24G-006 — TCP forward/reverse throughput baseline

Source: `wifi_vd_tcs.ods` · AUTO

**Critical:** Forward receiver throughput → >= 5 Mbps; Reverse receiver throughput → >= 5 Mbps; Disconnects → 0; Required measurement capture → Source-requested measurements recorded

**Supporting:** None

**Informational/context:** Retransmits; RSSI; Upload; Download

**Hard dependencies:** Shared SSH; Control interface; Wi-Fi interface; nmcli; NetworkManager; iw; Current band; Valid network path; Full Wi-Fi qualification path; Required Wi-Fi association; Laptop Wi-Fi interface; Jetson AP IP; iperf3 local; iperf3 Jetson

**Optional dependencies:** AP profile; Wi-Fi PHY; Target band capability; Current Wi-Fi mode; Current SSID; Backup Ethernet; Auto reconnect; Self-disruption safety; Position confirmed >=1 m; Recorded >=1 m distance; Position confirmed 10 m; Recorded 10 m distance; Valid Wi-Fi credential; External 2.4G STA SSID; External STA credential; Max packet loss (%); Max UDP loss (%); Max UDP jitter (ms); Min UDP receiver (Mbps); Security requirement

**Source setup (tester verification):** Jetson boot ổn định; có SSH Ethernet dự phòng; AP profile được xác nhận; client test có Wi-Fi và iperf3; thay đổi band/channel/security chỉ theo procedure được phê duyệt; restore production config sau test.

## TC-JET-24G-007 — UDP 5 Mbps / 10 Mbps loss and jitter characterization

Source: `wifi_vd_tcs.ods` · AUTO

**Critical:** UDP 5 Mbps receiver → near 5 Mbps target (source is qualitative); UDP loss → low / 0% ideal (source is qualitative); UDP jitter → low (source is qualitative); 10 Mbps link → link remains connected; UDP 5 Mbps run → completed; UDP 10 Mbps run → completed; Network errors → no Network is unreachable; Unexpected disconnects → none; NetworkManager errors → none; Required measurement capture → Source-requested measurements recorded

**Supporting:** None

**Informational/context:** UDP 5 sender Mbps; UDP 5 lost datagrams; UDP 5 total datagrams; UDP 10 sender Mbps; UDP 10 lost datagrams; UDP 10 total datagrams; UDP 10 receiver Mbps; UDP 10 loss; UDP 10 jitter; RSSI; Upload; Download; Retransmits

**Hard dependencies:** Shared SSH; Control interface; Wi-Fi interface; Current band; Valid network path; Full Wi-Fi qualification path; Laptop Wi-Fi interface; Jetson AP IP; iperf3 local; iperf3 Jetson

**Optional dependencies:** AP profile; nmcli; NetworkManager; iw; Wi-Fi PHY; Target band capability; Current Wi-Fi mode; Current SSID; Required Wi-Fi association; Backup Ethernet; Auto reconnect; Self-disruption safety; Position confirmed >=1 m; Recorded >=1 m distance; Position confirmed 10 m; Recorded 10 m distance; Valid Wi-Fi credential; External 2.4G STA SSID; External STA credential; Max packet loss (%); Max UDP loss (%); Max UDP jitter (ms); Min UDP receiver (Mbps); Security requirement

**Source setup (tester verification):** Jetson boot ổn định; có SSH Ethernet dự phòng; AP profile được xác nhận; client test có Wi-Fi và iperf3; thay đổi band/channel/security chỉ theo procedure được phê duyệt; restore production config sau test.

## TC-JET-24G-008 — Factory range ≥1 m validation

Source: `wifi_vd_tcs.ods` · AUTO

**Critical:** SSID discovery → visible at confirmed >1 m; Association → connected; IPv4/route → valid; Ping → succeeds; SSH → succeeds; Disconnects → 0; Physical distance → >=1 m confirmed

**Supporting:** None

**Informational/context:** Interface; State; Address; RSSI

**Hard dependencies:** Shared SSH; Control interface; Wi-Fi interface; nmcli; NetworkManager; iw; Current SSID; Current band; Valid network path; Full Wi-Fi qualification path; Required Wi-Fi association; Laptop Wi-Fi interface; Jetson AP IP; Position confirmed >=1 m

**Optional dependencies:** AP profile; Wi-Fi PHY; Target band capability; Current Wi-Fi mode; iperf3 local; iperf3 Jetson; Backup Ethernet; Auto reconnect; Self-disruption safety; Recorded >=1 m distance; Position confirmed 10 m; Recorded 10 m distance; Valid Wi-Fi credential; External 2.4G STA SSID; External STA credential; Max packet loss (%); Max UDP loss (%); Max UDP jitter (ms); Min UDP receiver (Mbps); Security requirement

**Source setup (tester verification):** Jetson boot ổn định; có SSH Ethernet dự phòng; AP profile được xác nhận; client test có Wi-Fi và iperf3; thay đổi band/channel/security chỉ theo procedure được phê duyệt; restore production config sau test.

## TC-JET-24G-009 — Throughput ≥5 Mbps at 10 m

Source: `wifi_vd_tcs.ods` · AUTO

**Critical:** Forward receiver throughput → >= 5 Mbps at 10 m; Reverse receiver throughput → >= 5 Mbps at 10 m; Disconnects → 0; Physical distance → 10 m confirmed

**Supporting:** None

**Informational/context:** RSSI; Upload; Download; Retransmits

**Hard dependencies:** Shared SSH; Control interface; Wi-Fi interface; nmcli; NetworkManager; iw; Current band; Valid network path; Full Wi-Fi qualification path; Required Wi-Fi association; Laptop Wi-Fi interface; Jetson AP IP; iperf3 local; iperf3 Jetson; Position confirmed 10 m

**Optional dependencies:** AP profile; Wi-Fi PHY; Target band capability; Current Wi-Fi mode; Current SSID; Backup Ethernet; Auto reconnect; Self-disruption safety; Position confirmed >=1 m; Recorded >=1 m distance; Recorded 10 m distance; Valid Wi-Fi credential; External 2.4G STA SSID; External STA credential; Max packet loss (%); Max UDP loss (%); Max UDP jitter (ms); Min UDP receiver (Mbps); Security requirement

**Source setup (tester verification):** Jetson boot ổn định; có SSH Ethernet dự phòng; AP profile được xác nhận; client test có Wi-Fi và iperf3; thay đổi band/channel/security chỉ theo procedure được phê duyệt; restore production config sau test.

## TC-JET-24G-010 — WPA2 / WPA3 compliance validation

Source: `wifi_vd_tcs.ods` · AUTO

**Critical:** WPA2 runtime → enabled; SAE capability → available for WPA3; WPA3 activation → successful; Legacy/open security → none

**Supporting:** None

**Informational/context:** Interface; State; Address; RSSI

**Hard dependencies:** Shared SSH; Control interface; Wi-Fi interface; nmcli; NetworkManager; iw; Current SSID

**Optional dependencies:** AP profile; Wi-Fi PHY; Target band capability; Current Wi-Fi mode; Current band; Valid network path; Full Wi-Fi qualification path; Required Wi-Fi association; Laptop Wi-Fi interface; Jetson AP IP; iperf3 local; iperf3 Jetson; Backup Ethernet; Auto reconnect; Self-disruption safety; Position confirmed >=1 m; Recorded >=1 m distance; Position confirmed 10 m; Recorded 10 m distance; Valid Wi-Fi credential; External 2.4G STA SSID; External STA credential; Max packet loss (%); Max UDP loss (%); Max UDP jitter (ms); Min UDP receiver (Mbps); Security requirement

**Source setup (tester verification):** Jetson boot ổn định; có SSH Ethernet dự phòng; AP profile được xác nhận; client test có Wi-Fi và iperf3; thay đổi band/channel/security chỉ theo procedure được phê duyệt; restore production config sau test.

## TC-JET-24G-011 — Valid/invalid credential + DHCP/reconnect validation

Source: `wifi_vd_tcs.ods` · AUTO

**Critical:** Valid credential → connects; Invalid credential → rejected; Temporary profile cleanup → deleted; Valid profile restoration → association, IPv4, route, reachability; DHCP → valid IPv4; Reconnect cycles → 5/5; Duplicate/stale IP → none; Phase A — Recovery cycle 1 → association, IPv4, Wi-Fi route and DUT reachability restored; Phase A — Recovery cycle 2 → association, IPv4, Wi-Fi route and DUT reachability restored; Phase A — Recovery cycle 3 → association, IPv4, Wi-Fi route and DUT reachability restored; Phase A — Recovery cycle 4 → association, IPv4, Wi-Fi route and DUT reachability restored; Phase A — Recovery cycle 5 → association, IPv4, Wi-Fi route and DUT reachability restored

**Supporting:** None

**Informational/context:** Completed cycles; Successful cycles; Avg recovery; Failures

**Hard dependencies:** Shared SSH; Control interface; Wi-Fi interface; nmcli; NetworkManager; iw; Current SSID; Current band; Valid network path; Full Wi-Fi qualification path; Required Wi-Fi association; Laptop Wi-Fi interface; Jetson AP IP; Disruptive execution authorized; Laptop NetworkManager; Saved valid NM profile; Recovery cycles / OFF duration; Phase A — Client recovery strategy; DUT AP mode

**Optional dependencies:** AP profile; Wi-Fi PHY; Target band capability; Current Wi-Fi mode; iperf3 local; iperf3 Jetson; Backup Ethernet; Auto reconnect; Self-disruption safety; Position confirmed >=1 m; Recorded >=1 m distance; Position confirmed 10 m; Recorded 10 m distance; Valid Wi-Fi credential; External 2.4G STA SSID; External STA credential; Max packet loss (%); Max UDP loss (%); Max UDP jitter (ms); Min UDP receiver (Mbps); Security requirement; Valid Wi-Fi access capability

**Source setup (tester verification):** Jetson boot ổn định; có SSH Ethernet dự phòng; AP profile được xác nhận; client test có Wi-Fi và iperf3; thay đổi band/channel/security chỉ theo procedure được phê duyệt; restore production config sau test.

## TC-JET-24G-012 — AP/client recovery validation

Source: `wifi_vd_tcs.ods` · AUTO

**Critical:** Phase A — Client recovery → 5/5; Phase B — AP recovery → restored; DHCP/route/service → restored; Reboot required → no; Phase A — Recovery cycle 1 → association, IPv4, Wi-Fi route and DUT reachability restored; Phase A — Recovery cycle 2 → association, IPv4, Wi-Fi route and DUT reachability restored; Phase A — Recovery cycle 3 → association, IPv4, Wi-Fi route and DUT reachability restored; Phase A — Recovery cycle 4 → association, IPv4, Wi-Fi route and DUT reachability restored; Phase A — Recovery cycle 5 → association, IPv4, Wi-Fi route and DUT reachability restored

**Supporting:** None

**Informational/context:** Completed cycles; Successful cycles; Avg recovery; Failures

**Hard dependencies:** Shared SSH; Control interface; Wi-Fi interface; AP profile; nmcli; NetworkManager; iw; Current SSID; Current band; Valid network path; Full Wi-Fi qualification path; Required Wi-Fi association; Laptop Wi-Fi interface; Jetson AP IP; Disruptive execution authorized; Laptop NetworkManager; Saved valid NM profile; Recovery cycles / OFF duration; Phase A — Client recovery strategy; DUT AP mode; Phase B — AP restart strategy

**Optional dependencies:** Wi-Fi PHY; Target band capability; Current Wi-Fi mode; iperf3 local; iperf3 Jetson; Backup Ethernet; Auto reconnect; Self-disruption safety; Position confirmed >=1 m; Recorded >=1 m distance; Position confirmed 10 m; Recorded 10 m distance; Valid Wi-Fi credential; External 2.4G STA SSID; External STA credential; Max packet loss (%); Max UDP loss (%); Max UDP jitter (ms); Min UDP receiver (Mbps); Security requirement; Valid Wi-Fi access capability

**Source setup (tester verification):** Jetson boot ổn định; có SSH Ethernet dự phòng; AP profile được xác nhận; client test có Wi-Fi và iperf3; thay đổi band/channel/security chỉ theo procedure được phê duyệt; restore production config sau test.

## TC-JET-24G-013 — 2-hour band-specific AP endurance

Source: `wifi_vd_tcs.ods` · AUTO

**Critical:** Endurance duration → >= 7200 s; Prolonged SSID loss → none; Manual recovery → none; NetworkManager failures → none; Driver resets → none; Service failures → none

**Supporting:** None

**Informational/context:** Elapsed; Min RSSI; Errors

**Hard dependencies:** Shared SSH; Control interface; Wi-Fi interface; nmcli; NetworkManager; iw; Current SSID; Current band; Valid network path; Full Wi-Fi qualification path; Required Wi-Fi association; Laptop Wi-Fi interface; Jetson AP IP

**Optional dependencies:** AP profile; Wi-Fi PHY; Target band capability; Current Wi-Fi mode; iperf3 local; iperf3 Jetson; Backup Ethernet; Auto reconnect; Self-disruption safety; Position confirmed >=1 m; Recorded >=1 m distance; Position confirmed 10 m; Recorded 10 m distance; Valid Wi-Fi credential; External 2.4G STA SSID; External STA credential; Max packet loss (%); Max UDP loss (%); Max UDP jitter (ms); Min UDP receiver (Mbps); Security requirement

**Source setup (tester verification):** Jetson boot ổn định; có SSH Ethernet dự phòng; AP profile được xác nhận; client test có Wi-Fi và iperf3; thay đổi band/channel/security chỉ theo procedure được phê duyệt; restore production config sau test.

## TC-JET-5G-001 — AP runtime interface / SSID / mode / band validation

Source: `wifi_vd_tcs.ods` · AUTO

**Critical:** AP mode → AP; Band → 5 GHz; Interface → UP; AP IPv4 → valid IPv4; Regulatory/activation errors → none

**Supporting:** Channel → Valid 5 GHz channel; Frequency → Consistent with selected channel / band

**Informational/context:** SSID; BSSID; Driver; PHY; Tx power; Width; Interface; State; Address; RSSI

**Hard dependencies:** Shared SSH; Control interface; Wi-Fi interface; nmcli; NetworkManager; iw; Current Wi-Fi mode; Current SSID; Current band

**Optional dependencies:** AP profile; Wi-Fi PHY; Target band capability; Valid network path; Full Wi-Fi qualification path; Required Wi-Fi association; Laptop Wi-Fi interface; Jetson AP IP; iperf3 local; iperf3 Jetson; Backup Ethernet; Auto reconnect; Self-disruption safety; Position confirmed >=1 m; Recorded >=1 m distance; Position confirmed 10 m; Recorded 10 m distance; Valid Wi-Fi credential; External 5G STA SSID; External STA credential; Max packet loss (%); Max UDP loss (%); Max UDP jitter (ms); Min UDP receiver (Mbps); Security requirement

**Source setup (tester verification):** Jetson boot ổn định; có SSH Ethernet dự phòng; AP profile được xác nhận; client test có Wi-Fi và iperf3; thay đổi band/channel/security chỉ theo procedure được phê duyệt; restore production config sau test.

## TC-JET-5G-002 — Wi-Fi 5/6 capability and runtime PHY validation

Source: `wifi_vd_tcs.ods` · AUTO

**Critical:** PHY capability → Wi-Fi 5 / 802.11ac / VHT or Wi-Fi 6 / 802.11ax / HE; Runtime PHY → Wi-Fi 5 / 802.11ac / VHT or Wi-Fi 6 / 802.11ax / HE

**Supporting:** None

**Informational/context:** SSID; BSSID; Channel; RSSI

**Hard dependencies:** Shared SSH; Control interface; Wi-Fi interface; iw; Wi-Fi PHY; Target band capability

**Optional dependencies:** AP profile; nmcli; NetworkManager; Current Wi-Fi mode; Current SSID; Current band; Valid network path; Full Wi-Fi qualification path; Required Wi-Fi association; Laptop Wi-Fi interface; Jetson AP IP; iperf3 local; iperf3 Jetson; Backup Ethernet; Auto reconnect; Self-disruption safety; Position confirmed >=1 m; Recorded >=1 m distance; Position confirmed 10 m; Recorded 10 m distance; Valid Wi-Fi credential; External 5G STA SSID; External STA credential; Max packet loss (%); Max UDP loss (%); Max UDP jitter (ms); Min UDP receiver (Mbps); Security requirement

**Source setup (tester verification):** Jetson boot ổn định; có SSH Ethernet dự phòng; AP profile được xác nhận; client test có Wi-Fi và iperf3; thay đổi band/channel/security chỉ theo procedure được phê duyệt; restore production config sau test.

## TC-JET-5G-003 — Client association, IPv4 and Wi-Fi route validation

Source: `wifi_vd_tcs.ods` · AUTO

**Critical:** Association → client associated with target Jetson AP SSID (runtime-discovered); Band → 5 GHz; Client IPv4 → valid IPv4; Wi-Fi route → route to DUT_AP_IP uses CLIENT_WIFI_IF; Reachability → DUT_AP_IP reachable through CLIENT_WIFI_IF

**Supporting:** None

**Informational/context:** Interface; State; Address; RSSI

**Hard dependencies:** Shared SSH; Control interface; Wi-Fi interface; nmcli; NetworkManager; iw; Current SSID; Current band; Laptop Wi-Fi interface; Jetson AP IP

**Optional dependencies:** AP profile; Wi-Fi PHY; Target band capability; Current Wi-Fi mode; iperf3 local; iperf3 Jetson; Backup Ethernet; Auto reconnect; Self-disruption safety; Position confirmed >=1 m; Recorded >=1 m distance; Position confirmed 10 m; Recorded 10 m distance; Valid Wi-Fi credential; External 5G STA SSID; External STA credential; Max packet loss (%); Max UDP loss (%); Max UDP jitter (ms); Min UDP receiver (Mbps); Security requirement

**Source setup (tester verification):** Jetson boot ổn định; có SSH Ethernet dự phòng; AP profile được xác nhận; client test có Wi-Fi và iperf3; thay đổi band/channel/security chỉ theo procedure được phê duyệt; restore production config sau test.

## TC-JET-5G-004 — RSSI and PHY bitrate validation

Source: `wifi_vd_tcs.ods` · AUTO

**Critical:** Disconnects → 0; Required measurement capture → Source-requested measurements recorded

**Supporting:** None

**Informational/context:** RSSI; TX PHY bitrate; RX PHY bitrate; SSID; BSSID; Channel

**Hard dependencies:** Shared SSH; Control interface; Wi-Fi interface; iw; Wi-Fi PHY; Target band capability; Current band; Required Wi-Fi association; Laptop Wi-Fi interface

**Optional dependencies:** AP profile; nmcli; NetworkManager; Current Wi-Fi mode; Current SSID; Valid network path; Full Wi-Fi qualification path; Jetson AP IP; iperf3 local; iperf3 Jetson; Backup Ethernet; Auto reconnect; Self-disruption safety; Position confirmed >=1 m; Recorded >=1 m distance; Position confirmed 10 m; Recorded 10 m distance; Valid Wi-Fi credential; External 5G STA SSID; External STA credential; Max packet loss (%); Max UDP loss (%); Max UDP jitter (ms); Min UDP receiver (Mbps); Security requirement

**Source setup (tester verification):** Jetson boot ổn định; có SSH Ethernet dự phòng; AP profile được xác nhận; client test có Wi-Fi và iperf3; thay đổi band/channel/security chỉ theo procedure được phê duyệt; restore production config sau test.

## TC-JET-5G-005 — Local RTT latency and packet-loss validation

Source: `wifi_vd_tcs.ods` · AUTO

**Critical:** Packets sent → collected; Packets received → collected; Average RTT → <= 100 ms; Packet loss → not abnormal (source is qualitative); Disconnects → 0; Required measurement capture → Source-requested measurements recorded

**Supporting:** None

**Informational/context:** RTT min; RTT max; RTT mdev; RTT; Average RTT; Jitter

**Hard dependencies:** Shared SSH; Control interface; Wi-Fi interface; nmcli; NetworkManager; iw; Current band; Valid network path; Full Wi-Fi qualification path; Required Wi-Fi association; Laptop Wi-Fi interface; Jetson AP IP

**Optional dependencies:** AP profile; Wi-Fi PHY; Target band capability; Current Wi-Fi mode; Current SSID; iperf3 local; iperf3 Jetson; Backup Ethernet; Auto reconnect; Self-disruption safety; Position confirmed >=1 m; Recorded >=1 m distance; Position confirmed 10 m; Recorded 10 m distance; Valid Wi-Fi credential; External 5G STA SSID; External STA credential; Max packet loss (%); Max UDP loss (%); Max UDP jitter (ms); Min UDP receiver (Mbps); Security requirement

**Source setup (tester verification):** Jetson boot ổn định; có SSH Ethernet dự phòng; AP profile được xác nhận; client test có Wi-Fi và iperf3; thay đổi band/channel/security chỉ theo procedure được phê duyệt; restore production config sau test.

## TC-JET-5G-006 — TCP forward/reverse throughput baseline

Source: `wifi_vd_tcs.ods` · AUTO

**Critical:** Forward receiver throughput → >= 5 Mbps; Reverse receiver throughput → >= 5 Mbps; Disconnects → 0; Required measurement capture → Source-requested measurements recorded

**Supporting:** None

**Informational/context:** Retransmits; RSSI; Upload; Download

**Hard dependencies:** Shared SSH; Control interface; Wi-Fi interface; nmcli; NetworkManager; iw; Current band; Valid network path; Full Wi-Fi qualification path; Required Wi-Fi association; Laptop Wi-Fi interface; Jetson AP IP; iperf3 local; iperf3 Jetson

**Optional dependencies:** AP profile; Wi-Fi PHY; Target band capability; Current Wi-Fi mode; Current SSID; Backup Ethernet; Auto reconnect; Self-disruption safety; Position confirmed >=1 m; Recorded >=1 m distance; Position confirmed 10 m; Recorded 10 m distance; Valid Wi-Fi credential; External 5G STA SSID; External STA credential; Max packet loss (%); Max UDP loss (%); Max UDP jitter (ms); Min UDP receiver (Mbps); Security requirement

**Source setup (tester verification):** Jetson boot ổn định; có SSH Ethernet dự phòng; AP profile được xác nhận; client test có Wi-Fi và iperf3; thay đổi band/channel/security chỉ theo procedure được phê duyệt; restore production config sau test.

## TC-JET-5G-007 — UDP 5 Mbps / 10 Mbps loss and jitter characterization

Source: `wifi_vd_tcs.ods` · AUTO

**Critical:** UDP 5 Mbps receiver → near 5 Mbps target (source is qualitative); UDP loss → low / 0% ideal (source is qualitative); UDP jitter → low (source is qualitative); 10 Mbps link → link remains connected; UDP 5 Mbps run → completed; UDP 10 Mbps run → completed; Network errors → no Network is unreachable; Unexpected disconnects → none; NetworkManager errors → none; Required measurement capture → Source-requested measurements recorded

**Supporting:** None

**Informational/context:** UDP 5 sender Mbps; UDP 5 lost datagrams; UDP 5 total datagrams; UDP 10 sender Mbps; UDP 10 lost datagrams; UDP 10 total datagrams; UDP 10 receiver Mbps; UDP 10 loss; UDP 10 jitter; RSSI; Upload; Download; Retransmits

**Hard dependencies:** Shared SSH; Control interface; Wi-Fi interface; Current band; Valid network path; Full Wi-Fi qualification path; Laptop Wi-Fi interface; Jetson AP IP; iperf3 local; iperf3 Jetson

**Optional dependencies:** AP profile; nmcli; NetworkManager; iw; Wi-Fi PHY; Target band capability; Current Wi-Fi mode; Current SSID; Required Wi-Fi association; Backup Ethernet; Auto reconnect; Self-disruption safety; Position confirmed >=1 m; Recorded >=1 m distance; Position confirmed 10 m; Recorded 10 m distance; Valid Wi-Fi credential; External 5G STA SSID; External STA credential; Max packet loss (%); Max UDP loss (%); Max UDP jitter (ms); Min UDP receiver (Mbps); Security requirement

**Source setup (tester verification):** Jetson boot ổn định; có SSH Ethernet dự phòng; AP profile được xác nhận; client test có Wi-Fi và iperf3; thay đổi band/channel/security chỉ theo procedure được phê duyệt; restore production config sau test.

## TC-JET-5G-008 — Factory range ≥1 m validation

Source: `wifi_vd_tcs.ods` · AUTO

**Critical:** SSID discovery → visible at confirmed >1 m; Association → connected; IPv4/route → valid; Ping → succeeds; SSH → succeeds; Disconnects → 0; Physical distance → >=1 m confirmed

**Supporting:** None

**Informational/context:** Interface; State; Address; RSSI

**Hard dependencies:** Shared SSH; Control interface; Wi-Fi interface; nmcli; NetworkManager; iw; Current SSID; Current band; Valid network path; Full Wi-Fi qualification path; Required Wi-Fi association; Laptop Wi-Fi interface; Jetson AP IP; Position confirmed >=1 m

**Optional dependencies:** AP profile; Wi-Fi PHY; Target band capability; Current Wi-Fi mode; iperf3 local; iperf3 Jetson; Backup Ethernet; Auto reconnect; Self-disruption safety; Recorded >=1 m distance; Position confirmed 10 m; Recorded 10 m distance; Valid Wi-Fi credential; External 5G STA SSID; External STA credential; Max packet loss (%); Max UDP loss (%); Max UDP jitter (ms); Min UDP receiver (Mbps); Security requirement

**Source setup (tester verification):** Jetson boot ổn định; có SSH Ethernet dự phòng; AP profile được xác nhận; client test có Wi-Fi và iperf3; thay đổi band/channel/security chỉ theo procedure được phê duyệt; restore production config sau test.

## TC-JET-5G-009 — Throughput ≥5 Mbps at 10 m

Source: `wifi_vd_tcs.ods` · AUTO

**Critical:** Forward receiver throughput → >= 5 Mbps at 10 m; Reverse receiver throughput → >= 5 Mbps at 10 m; Disconnects → 0; Physical distance → 10 m confirmed

**Supporting:** None

**Informational/context:** RSSI; Upload; Download; Retransmits

**Hard dependencies:** Shared SSH; Control interface; Wi-Fi interface; nmcli; NetworkManager; iw; Current band; Valid network path; Full Wi-Fi qualification path; Required Wi-Fi association; Laptop Wi-Fi interface; Jetson AP IP; iperf3 local; iperf3 Jetson; Position confirmed 10 m

**Optional dependencies:** AP profile; Wi-Fi PHY; Target band capability; Current Wi-Fi mode; Current SSID; Backup Ethernet; Auto reconnect; Self-disruption safety; Position confirmed >=1 m; Recorded >=1 m distance; Recorded 10 m distance; Valid Wi-Fi credential; External 5G STA SSID; External STA credential; Max packet loss (%); Max UDP loss (%); Max UDP jitter (ms); Min UDP receiver (Mbps); Security requirement

**Source setup (tester verification):** Jetson boot ổn định; có SSH Ethernet dự phòng; AP profile được xác nhận; client test có Wi-Fi và iperf3; thay đổi band/channel/security chỉ theo procedure được phê duyệt; restore production config sau test.

## TC-JET-5G-010 — WPA2 / WPA3 compliance validation

Source: `wifi_vd_tcs.ods` · AUTO

**Critical:** WPA2 runtime → enabled; SAE capability → available for WPA3; WPA3 activation → successful; Legacy/open security → none

**Supporting:** None

**Informational/context:** Interface; State; Address; RSSI

**Hard dependencies:** Shared SSH; Control interface; Wi-Fi interface; nmcli; NetworkManager; iw; Current SSID

**Optional dependencies:** AP profile; Wi-Fi PHY; Target band capability; Current Wi-Fi mode; Current band; Valid network path; Full Wi-Fi qualification path; Required Wi-Fi association; Laptop Wi-Fi interface; Jetson AP IP; iperf3 local; iperf3 Jetson; Backup Ethernet; Auto reconnect; Self-disruption safety; Position confirmed >=1 m; Recorded >=1 m distance; Position confirmed 10 m; Recorded 10 m distance; Valid Wi-Fi credential; External 5G STA SSID; External STA credential; Max packet loss (%); Max UDP loss (%); Max UDP jitter (ms); Min UDP receiver (Mbps); Security requirement

**Source setup (tester verification):** Jetson boot ổn định; có SSH Ethernet dự phòng; AP profile được xác nhận; client test có Wi-Fi và iperf3; thay đổi band/channel/security chỉ theo procedure được phê duyệt; restore production config sau test.

## TC-JET-5G-011 — Valid/invalid credential + DHCP/reconnect validation

Source: `wifi_vd_tcs.ods` · AUTO

**Critical:** Valid credential → connects; Invalid credential → rejected; Temporary profile cleanup → deleted; Valid profile restoration → association, IPv4, route, reachability; DHCP → valid IPv4; Reconnect cycles → 5/5; Duplicate/stale IP → none; Phase A — Recovery cycle 1 → association, IPv4, Wi-Fi route and DUT reachability restored; Phase A — Recovery cycle 2 → association, IPv4, Wi-Fi route and DUT reachability restored; Phase A — Recovery cycle 3 → association, IPv4, Wi-Fi route and DUT reachability restored; Phase A — Recovery cycle 4 → association, IPv4, Wi-Fi route and DUT reachability restored; Phase A — Recovery cycle 5 → association, IPv4, Wi-Fi route and DUT reachability restored

**Supporting:** None

**Informational/context:** Completed cycles; Successful cycles; Avg recovery; Failures

**Hard dependencies:** Shared SSH; Control interface; Wi-Fi interface; nmcli; NetworkManager; iw; Current SSID; Current band; Valid network path; Full Wi-Fi qualification path; Required Wi-Fi association; Laptop Wi-Fi interface; Jetson AP IP; Disruptive execution authorized; Laptop NetworkManager; Saved valid NM profile; Recovery cycles / OFF duration; Phase A — Client recovery strategy; DUT AP mode

**Optional dependencies:** AP profile; Wi-Fi PHY; Target band capability; Current Wi-Fi mode; iperf3 local; iperf3 Jetson; Backup Ethernet; Auto reconnect; Self-disruption safety; Position confirmed >=1 m; Recorded >=1 m distance; Position confirmed 10 m; Recorded 10 m distance; Valid Wi-Fi credential; External 5G STA SSID; External STA credential; Max packet loss (%); Max UDP loss (%); Max UDP jitter (ms); Min UDP receiver (Mbps); Security requirement; Valid Wi-Fi access capability

**Source setup (tester verification):** Jetson boot ổn định; có SSH Ethernet dự phòng; AP profile được xác nhận; client test có Wi-Fi và iperf3; thay đổi band/channel/security chỉ theo procedure được phê duyệt; restore production config sau test.

## TC-JET-5G-012 — AP/client recovery validation

Source: `wifi_vd_tcs.ods` · AUTO

**Critical:** Phase A — Client recovery → 5/5; Phase B — AP recovery → restored; DHCP/route/service → restored; Reboot required → no; Phase A — Recovery cycle 1 → association, IPv4, Wi-Fi route and DUT reachability restored; Phase A — Recovery cycle 2 → association, IPv4, Wi-Fi route and DUT reachability restored; Phase A — Recovery cycle 3 → association, IPv4, Wi-Fi route and DUT reachability restored; Phase A — Recovery cycle 4 → association, IPv4, Wi-Fi route and DUT reachability restored; Phase A — Recovery cycle 5 → association, IPv4, Wi-Fi route and DUT reachability restored

**Supporting:** None

**Informational/context:** Completed cycles; Successful cycles; Avg recovery; Failures

**Hard dependencies:** Shared SSH; Control interface; Wi-Fi interface; AP profile; nmcli; NetworkManager; iw; Current SSID; Current band; Valid network path; Full Wi-Fi qualification path; Required Wi-Fi association; Laptop Wi-Fi interface; Jetson AP IP; Disruptive execution authorized; Laptop NetworkManager; Saved valid NM profile; Recovery cycles / OFF duration; Phase A — Client recovery strategy; DUT AP mode; Phase B — AP restart strategy

**Optional dependencies:** Wi-Fi PHY; Target band capability; Current Wi-Fi mode; iperf3 local; iperf3 Jetson; Backup Ethernet; Auto reconnect; Self-disruption safety; Position confirmed >=1 m; Recorded >=1 m distance; Position confirmed 10 m; Recorded 10 m distance; Valid Wi-Fi credential; External 5G STA SSID; External STA credential; Max packet loss (%); Max UDP loss (%); Max UDP jitter (ms); Min UDP receiver (Mbps); Security requirement; Valid Wi-Fi access capability

**Source setup (tester verification):** Jetson boot ổn định; có SSH Ethernet dự phòng; AP profile được xác nhận; client test có Wi-Fi và iperf3; thay đổi band/channel/security chỉ theo procedure được phê duyệt; restore production config sau test.

## TC-JET-5G-013 — 2-hour band-specific AP endurance

Source: `wifi_vd_tcs.ods` · AUTO

**Critical:** Endurance duration → >= 7200 s; Prolonged SSID loss → none; Manual recovery → none; NetworkManager failures → none; Driver resets → none; Service failures → none

**Supporting:** None

**Informational/context:** Elapsed; Min RSSI; Errors

**Hard dependencies:** Shared SSH; Control interface; Wi-Fi interface; nmcli; NetworkManager; iw; Current SSID; Current band; Valid network path; Full Wi-Fi qualification path; Required Wi-Fi association; Laptop Wi-Fi interface; Jetson AP IP

**Optional dependencies:** AP profile; Wi-Fi PHY; Target band capability; Current Wi-Fi mode; iperf3 local; iperf3 Jetson; Backup Ethernet; Auto reconnect; Self-disruption safety; Position confirmed >=1 m; Recorded >=1 m distance; Position confirmed 10 m; Recorded 10 m distance; Valid Wi-Fi credential; External 5G STA SSID; External STA credential; Max packet loss (%); Max UDP loss (%); Max UDP jitter (ms); Min UDP receiver (Mbps); Security requirement

**Source setup (tester verification):** Jetson boot ổn định; có SSH Ethernet dự phòng; AP profile được xác nhận; client test có Wi-Fi và iperf3; thay đổi band/channel/security chỉ theo procedure được phê duyệt; restore production config sau test.

## TC-JET-STA-24G-001 — Jetson connects to external Wi-Fi (2.4 GHz)

Source: `wifi_vd_tcs.ods` · AUTO

**Critical:** Band → 2.4 GHz; Station mode → managed; IPv4 → valid; Gateway → valid; Wi-Fi route → present; Required measurement capture → Source-requested measurements recorded

**Supporting:** None

**Informational/context:** SSID; Interface; State; Address; RSSI

**Hard dependencies:** Shared SSH; Control interface; Wi-Fi interface; nmcli; NetworkManager; iw; Self-disruption safety; External 2.4G STA SSID; External STA credential

**Optional dependencies:** AP profile; Wi-Fi PHY; Target band capability; Current Wi-Fi mode; Current SSID; Current band; Valid network path; Full Wi-Fi qualification path; Required Wi-Fi association; Laptop Wi-Fi interface; Jetson AP IP; iperf3 local; iperf3 Jetson; Backup Ethernet; Auto reconnect; Position confirmed >=1 m; Recorded >=1 m distance; Position confirmed 10 m; Recorded 10 m distance; Valid Wi-Fi credential; Max packet loss (%); Max UDP loss (%); Max UDP jitter (ms); Min UDP receiver (Mbps); Security requirement

**Source setup (tester verification):** Jetson và Laptop Test đều nằm trong vùng phủ của cùng external Wi-Fi AP; external AP đang phát 2.4 GHz; laptop đã kết nối vào cùng SSID/subnet; có Ethernet/console backup trước mọi thao tác có thể làm mất Wi-Fi DUT; iperf3 có trên laptop và Jetson.

## TC-JET-STA-24G-002 — SSH to Jetson through the connected Wi-Fi

Source: `wifi_vd_tcs.ods` · AUTO

**Critical:** Ping → success through Wi-Fi; One-shot SSH → login succeeds; Route → uses client Wi-Fi interface; Host identity → expected Jetson host

**Supporting:** None

**Informational/context:** Interface; State; Address; RSSI

**Hard dependencies:** Shared SSH; Control interface; Wi-Fi interface; iw; Valid network path; Full Wi-Fi qualification path; Laptop Wi-Fi interface; Jetson AP IP; External 2.4G STA SSID

**Optional dependencies:** AP profile; nmcli; NetworkManager; Wi-Fi PHY; Target band capability; Current Wi-Fi mode; Current SSID; Current band; Required Wi-Fi association; iperf3 local; iperf3 Jetson; Backup Ethernet; Auto reconnect; Self-disruption safety; Position confirmed >=1 m; Recorded >=1 m distance; Position confirmed 10 m; Recorded 10 m distance; Valid Wi-Fi credential; External STA credential; Max packet loss (%); Max UDP loss (%); Max UDP jitter (ms); Min UDP receiver (Mbps); Security requirement

**Source setup (tester verification):** Jetson và Laptop Test đều nằm trong vùng phủ của cùng external Wi-Fi AP; external AP đang phát 2.4 GHz; laptop đã kết nối vào cùng SSID/subnet; có Ethernet/console backup trước mọi thao tác có thể làm mất Wi-Fi DUT; iperf3 có trên laptop và Jetson.

## TC-JET-STA-24G-003 — Check SSID/BSSID/channel/frequency/RSSI/PHY bitrate

Source: `wifi_vd_tcs.ods` · AUTO

**Critical:** SSID/BSSID → external AP matches; Frequency → 2.4 GHz; RSSI samples → 10 samples; TX/RX bitrate → collected; Disconnects → 0

**Supporting:** None

**Informational/context:** BSSID; Driver; PHY; Tx power; Width; SSID; Channel; RSSI

**Hard dependencies:** Shared SSH; Control interface; Wi-Fi interface; nmcli; NetworkManager; iw; External 2.4G STA SSID

**Optional dependencies:** AP profile; Wi-Fi PHY; Target band capability; Current Wi-Fi mode; Current SSID; Current band; Valid network path; Full Wi-Fi qualification path; Required Wi-Fi association; Laptop Wi-Fi interface; Jetson AP IP; iperf3 local; iperf3 Jetson; Backup Ethernet; Auto reconnect; Self-disruption safety; Position confirmed >=1 m; Recorded >=1 m distance; Position confirmed 10 m; Recorded 10 m distance; Valid Wi-Fi credential; External STA credential; Max packet loss (%); Max UDP loss (%); Max UDP jitter (ms); Min UDP receiver (Mbps); Security requirement

**Source setup (tester verification):** Jetson và Laptop Test đều nằm trong vùng phủ của cùng external Wi-Fi AP; external AP đang phát 2.4 GHz; laptop đã kết nối vào cùng SSID/subnet; có Ethernet/console backup trước mọi thao tác có thể làm mất Wi-Fi DUT; iperf3 có trên laptop và Jetson.

## TC-JET-STA-24G-004 — Validate Wi-Fi 5 / Wi-Fi 6 capability and negotiated runtime

Source: `wifi_vd_tcs.ods` · AUTO

**Critical:** Capability → recorded; Wi-Fi 6 evidence → HE/802.11ax

**Supporting:** None

**Informational/context:** SSID; BSSID; Channel; RSSI

**Hard dependencies:** Shared SSH; Control interface; Wi-Fi interface; iw; Wi-Fi PHY; Target band capability; External 2.4G STA SSID

**Optional dependencies:** AP profile; nmcli; NetworkManager; Current Wi-Fi mode; Current SSID; Current band; Valid network path; Full Wi-Fi qualification path; Required Wi-Fi association; Laptop Wi-Fi interface; Jetson AP IP; iperf3 local; iperf3 Jetson; Backup Ethernet; Auto reconnect; Self-disruption safety; Position confirmed >=1 m; Recorded >=1 m distance; Position confirmed 10 m; Recorded 10 m distance; Valid Wi-Fi credential; External STA credential; Max packet loss (%); Max UDP loss (%); Max UDP jitter (ms); Min UDP receiver (Mbps); Security requirement

**Source setup (tester verification):** Jetson và Laptop Test đều nằm trong vùng phủ của cùng external Wi-Fi AP; external AP đang phát 2.4 GHz; laptop đã kết nối vào cùng SSID/subnet; có Ethernet/console backup trước mọi thao tác có thể làm mất Wi-Fi DUT; iperf3 có trên laptop và Jetson.

## TC-JET-STA-24G-005 — Measure RTT and packet loss through infrastructure Wi-Fi

Source: `wifi_vd_tcs.ods` · AUTO

**Critical:** Client↔DUT reachability → reachable; Average RTT → <= 100 ms; Packet loss → not abnormal (source is qualitative); Disconnects → 0

**Supporting:** None

**Informational/context:** RTT; Average RTT; Jitter

**Hard dependencies:** Shared SSH; Control interface; Wi-Fi interface; nmcli; NetworkManager; iw; Valid network path; Full Wi-Fi qualification path; Laptop Wi-Fi interface; Jetson AP IP; External 2.4G STA SSID

**Optional dependencies:** AP profile; Wi-Fi PHY; Target band capability; Current Wi-Fi mode; Current SSID; Current band; Required Wi-Fi association; iperf3 local; iperf3 Jetson; Backup Ethernet; Auto reconnect; Self-disruption safety; Position confirmed >=1 m; Recorded >=1 m distance; Position confirmed 10 m; Recorded 10 m distance; Valid Wi-Fi credential; External STA credential; Max packet loss (%); Max UDP loss (%); Max UDP jitter (ms); Min UDP receiver (Mbps); Security requirement

**Source setup (tester verification):** Jetson và Laptop Test đều nằm trong vùng phủ của cùng external Wi-Fi AP; external AP đang phát 2.4 GHz; laptop đã kết nối vào cùng SSID/subnet; có Ethernet/console backup trước mọi thao tác có thể làm mất Wi-Fi DUT; iperf3 có trên laptop và Jetson.

## TC-JET-STA-24G-006 — TCP/UDP throughput over the same external Wi-Fi

Source: `wifi_vd_tcs.ods` · AUTO

**Critical:** Forward receiver throughput → Recorded; 5 Mbps applies only to confirmed 10 m requirement; Reverse receiver throughput → Recorded; 5 Mbps applies only to confirmed 10 m requirement; Network errors → none; Disconnects → none; Required measurement capture → Source-requested measurements recorded

**Supporting:** None

**Informational/context:** UDP loss; UDP jitter; RSSI; Upload; Download; Retransmits

**Hard dependencies:** Shared SSH; Control interface; Wi-Fi interface; nmcli; NetworkManager; iw; Valid network path; Full Wi-Fi qualification path; Laptop Wi-Fi interface; Jetson AP IP; iperf3 local; iperf3 Jetson; External 2.4G STA SSID

**Optional dependencies:** AP profile; Wi-Fi PHY; Target band capability; Current Wi-Fi mode; Current SSID; Current band; Required Wi-Fi association; Backup Ethernet; Auto reconnect; Self-disruption safety; Position confirmed >=1 m; Recorded >=1 m distance; Position confirmed 10 m; Recorded 10 m distance; Valid Wi-Fi credential; External STA credential; Max packet loss (%); Max UDP loss (%); Max UDP jitter (ms); Min UDP receiver (Mbps); Security requirement

**Source setup (tester verification):** Jetson và Laptop Test đều nằm trong vùng phủ của cùng external Wi-Fi AP; external AP đang phát 2.4 GHz; laptop đã kết nối vào cùng SSID/subnet; có Ethernet/console backup trước mọi thao tác có thể làm mất Wi-Fi DUT; iperf3 có trên laptop và Jetson.

## TC-JET-STA-24G-007 — Validate WPA2/WPA3 of the connected infrastructure Wi-Fi

Source: `wifi_vd_tcs.ods` · AUTO

**Critical:** Runtime security → WPA2/WPA3; Insecure modes → none; Invalid credential → rejected; Valid reconnect → successful

**Supporting:** None

**Informational/context:** Interface; State; Address; RSSI

**Hard dependencies:** Shared SSH; Control interface; Wi-Fi interface; nmcli; NetworkManager; iw; Self-disruption safety; External 2.4G STA SSID; External STA credential; Security requirement

**Optional dependencies:** AP profile; Wi-Fi PHY; Target band capability; Current Wi-Fi mode; Current SSID; Current band; Valid network path; Full Wi-Fi qualification path; Required Wi-Fi association; Laptop Wi-Fi interface; Jetson AP IP; iperf3 local; iperf3 Jetson; Backup Ethernet; Auto reconnect; Position confirmed >=1 m; Recorded >=1 m distance; Position confirmed 10 m; Recorded 10 m distance; Valid Wi-Fi credential; Max packet loss (%); Max UDP loss (%); Max UDP jitter (ms); Min UDP receiver (Mbps)

**Source setup (tester verification):** Jetson và Laptop Test đều nằm trong vùng phủ của cùng external Wi-Fi AP; external AP đang phát 2.4 GHz; laptop đã kết nối vào cùng SSID/subnet; có Ethernet/console backup trước mọi thao tác có thể làm mất Wi-Fi DUT; iperf3 có trên laptop và Jetson.

## TC-JET-STA-24G-008 — SSH + Wi-Fi link stability endurance

Source: `wifi_vd_tcs.ods` · AUTO

**Critical:** Endurance duration → >= 1800 s (source minimum 30 minutes; 2 hours optional); Prolonged Wi-Fi loss → none; SSH drops → none; IP/route → remains valid; Driver/NM resets → none; Manual recovery → none

**Supporting:** None

**Informational/context:** Elapsed; Min RSSI; Errors

**Hard dependencies:** Shared SSH; Control interface; Wi-Fi interface; nmcli; NetworkManager; iw; Valid network path; Full Wi-Fi qualification path; Laptop Wi-Fi interface; Jetson AP IP; External 2.4G STA SSID

**Optional dependencies:** AP profile; Wi-Fi PHY; Target band capability; Current Wi-Fi mode; Current SSID; Current band; Required Wi-Fi association; iperf3 local; iperf3 Jetson; Backup Ethernet; Auto reconnect; Self-disruption safety; Position confirmed >=1 m; Recorded >=1 m distance; Position confirmed 10 m; Recorded 10 m distance; Valid Wi-Fi credential; External STA credential; Max packet loss (%); Max UDP loss (%); Max UDP jitter (ms); Min UDP receiver (Mbps); Security requirement

**Source setup (tester verification):** Jetson và Laptop Test đều nằm trong vùng phủ của cùng external Wi-Fi AP; external AP đang phát 2.4 GHz; laptop đã kết nối vào cùng SSID/subnet; có Ethernet/console backup trước mọi thao tác có thể làm mất Wi-Fi DUT; iperf3 có trên laptop và Jetson.

## TC-JET-STA-5G-001 — Jetson connects to external Wi-Fi (5 GHz)

Source: `wifi_vd_tcs.ods` · AUTO

**Critical:** Band → 5 GHz; Station mode → managed; IPv4 → valid; Gateway → valid; Wi-Fi route → present; Required measurement capture → Source-requested measurements recorded

**Supporting:** None

**Informational/context:** SSID; Interface; State; Address; RSSI

**Hard dependencies:** Shared SSH; Control interface; Wi-Fi interface; nmcli; NetworkManager; iw; Self-disruption safety; External 5G STA SSID; External STA credential

**Optional dependencies:** AP profile; Wi-Fi PHY; Target band capability; Current Wi-Fi mode; Current SSID; Current band; Valid network path; Full Wi-Fi qualification path; Required Wi-Fi association; Laptop Wi-Fi interface; Jetson AP IP; iperf3 local; iperf3 Jetson; Backup Ethernet; Auto reconnect; Position confirmed >=1 m; Recorded >=1 m distance; Position confirmed 10 m; Recorded 10 m distance; Valid Wi-Fi credential; Max packet loss (%); Max UDP loss (%); Max UDP jitter (ms); Min UDP receiver (Mbps); Security requirement

**Source setup (tester verification):** Jetson và Laptop Test đều nằm trong vùng phủ của cùng external Wi-Fi AP; external AP đang phát 5 GHz; laptop đã kết nối vào cùng SSID/subnet; có Ethernet/console backup trước mọi thao tác có thể làm mất Wi-Fi DUT; iperf3 có trên laptop và Jetson.

## TC-JET-STA-5G-002 — SSH to Jetson through the connected Wi-Fi

Source: `wifi_vd_tcs.ods` · AUTO

**Critical:** Ping → success through Wi-Fi; One-shot SSH → login succeeds; Route → uses client Wi-Fi interface; Host identity → expected Jetson host

**Supporting:** None

**Informational/context:** Interface; State; Address; RSSI

**Hard dependencies:** Shared SSH; Control interface; Wi-Fi interface; iw; Valid network path; Full Wi-Fi qualification path; Laptop Wi-Fi interface; Jetson AP IP; External 5G STA SSID

**Optional dependencies:** AP profile; nmcli; NetworkManager; Wi-Fi PHY; Target band capability; Current Wi-Fi mode; Current SSID; Current band; Required Wi-Fi association; iperf3 local; iperf3 Jetson; Backup Ethernet; Auto reconnect; Self-disruption safety; Position confirmed >=1 m; Recorded >=1 m distance; Position confirmed 10 m; Recorded 10 m distance; Valid Wi-Fi credential; External STA credential; Max packet loss (%); Max UDP loss (%); Max UDP jitter (ms); Min UDP receiver (Mbps); Security requirement

**Source setup (tester verification):** Jetson và Laptop Test đều nằm trong vùng phủ của cùng external Wi-Fi AP; external AP đang phát 5 GHz; laptop đã kết nối vào cùng SSID/subnet; có Ethernet/console backup trước mọi thao tác có thể làm mất Wi-Fi DUT; iperf3 có trên laptop và Jetson.

## TC-JET-STA-5G-003 — Check SSID/BSSID/channel/frequency/RSSI/PHY bitrate

Source: `wifi_vd_tcs.ods` · AUTO

**Critical:** SSID/BSSID → external AP matches; Frequency → 5 GHz; RSSI samples → 10 samples; TX/RX bitrate → collected; Disconnects → 0

**Supporting:** None

**Informational/context:** BSSID; Driver; PHY; Tx power; Width; SSID; Channel; RSSI

**Hard dependencies:** Shared SSH; Control interface; Wi-Fi interface; nmcli; NetworkManager; iw; External 5G STA SSID

**Optional dependencies:** AP profile; Wi-Fi PHY; Target band capability; Current Wi-Fi mode; Current SSID; Current band; Valid network path; Full Wi-Fi qualification path; Required Wi-Fi association; Laptop Wi-Fi interface; Jetson AP IP; iperf3 local; iperf3 Jetson; Backup Ethernet; Auto reconnect; Self-disruption safety; Position confirmed >=1 m; Recorded >=1 m distance; Position confirmed 10 m; Recorded 10 m distance; Valid Wi-Fi credential; External STA credential; Max packet loss (%); Max UDP loss (%); Max UDP jitter (ms); Min UDP receiver (Mbps); Security requirement

**Source setup (tester verification):** Jetson và Laptop Test đều nằm trong vùng phủ của cùng external Wi-Fi AP; external AP đang phát 5 GHz; laptop đã kết nối vào cùng SSID/subnet; có Ethernet/console backup trước mọi thao tác có thể làm mất Wi-Fi DUT; iperf3 có trên laptop và Jetson.

## TC-JET-STA-5G-004 — Validate Wi-Fi 5 / Wi-Fi 6 capability and negotiated runtime

Source: `wifi_vd_tcs.ods` · AUTO

**Critical:** Capability → recorded; Wi-Fi 6 evidence → HE/802.11ax

**Supporting:** None

**Informational/context:** SSID; BSSID; Channel; RSSI

**Hard dependencies:** Shared SSH; Control interface; Wi-Fi interface; iw; Wi-Fi PHY; Target band capability; External 5G STA SSID

**Optional dependencies:** AP profile; nmcli; NetworkManager; Current Wi-Fi mode; Current SSID; Current band; Valid network path; Full Wi-Fi qualification path; Required Wi-Fi association; Laptop Wi-Fi interface; Jetson AP IP; iperf3 local; iperf3 Jetson; Backup Ethernet; Auto reconnect; Self-disruption safety; Position confirmed >=1 m; Recorded >=1 m distance; Position confirmed 10 m; Recorded 10 m distance; Valid Wi-Fi credential; External STA credential; Max packet loss (%); Max UDP loss (%); Max UDP jitter (ms); Min UDP receiver (Mbps); Security requirement

**Source setup (tester verification):** Jetson và Laptop Test đều nằm trong vùng phủ của cùng external Wi-Fi AP; external AP đang phát 5 GHz; laptop đã kết nối vào cùng SSID/subnet; có Ethernet/console backup trước mọi thao tác có thể làm mất Wi-Fi DUT; iperf3 có trên laptop và Jetson.

## TC-JET-STA-5G-005 — Measure RTT and packet loss through infrastructure Wi-Fi

Source: `wifi_vd_tcs.ods` · AUTO

**Critical:** Client↔DUT reachability → reachable; Average RTT → <= 100 ms; Packet loss → not abnormal (source is qualitative); Disconnects → 0

**Supporting:** None

**Informational/context:** RTT; Average RTT; Jitter

**Hard dependencies:** Shared SSH; Control interface; Wi-Fi interface; nmcli; NetworkManager; iw; Valid network path; Full Wi-Fi qualification path; Laptop Wi-Fi interface; Jetson AP IP; External 5G STA SSID

**Optional dependencies:** AP profile; Wi-Fi PHY; Target band capability; Current Wi-Fi mode; Current SSID; Current band; Required Wi-Fi association; iperf3 local; iperf3 Jetson; Backup Ethernet; Auto reconnect; Self-disruption safety; Position confirmed >=1 m; Recorded >=1 m distance; Position confirmed 10 m; Recorded 10 m distance; Valid Wi-Fi credential; External STA credential; Max packet loss (%); Max UDP loss (%); Max UDP jitter (ms); Min UDP receiver (Mbps); Security requirement

**Source setup (tester verification):** Jetson và Laptop Test đều nằm trong vùng phủ của cùng external Wi-Fi AP; external AP đang phát 5 GHz; laptop đã kết nối vào cùng SSID/subnet; có Ethernet/console backup trước mọi thao tác có thể làm mất Wi-Fi DUT; iperf3 có trên laptop và Jetson.

## TC-JET-STA-5G-006 — TCP/UDP throughput over the same external Wi-Fi

Source: `wifi_vd_tcs.ods` · AUTO

**Critical:** Forward receiver throughput → Recorded; 5 Mbps applies only to confirmed 10 m requirement; Reverse receiver throughput → Recorded; 5 Mbps applies only to confirmed 10 m requirement; Network errors → none; Disconnects → none; Required measurement capture → Source-requested measurements recorded

**Supporting:** None

**Informational/context:** UDP loss; UDP jitter; RSSI; Upload; Download; Retransmits

**Hard dependencies:** Shared SSH; Control interface; Wi-Fi interface; nmcli; NetworkManager; iw; Valid network path; Full Wi-Fi qualification path; Laptop Wi-Fi interface; Jetson AP IP; iperf3 local; iperf3 Jetson; External 5G STA SSID

**Optional dependencies:** AP profile; Wi-Fi PHY; Target band capability; Current Wi-Fi mode; Current SSID; Current band; Required Wi-Fi association; Backup Ethernet; Auto reconnect; Self-disruption safety; Position confirmed >=1 m; Recorded >=1 m distance; Position confirmed 10 m; Recorded 10 m distance; Valid Wi-Fi credential; External STA credential; Max packet loss (%); Max UDP loss (%); Max UDP jitter (ms); Min UDP receiver (Mbps); Security requirement

**Source setup (tester verification):** Jetson và Laptop Test đều nằm trong vùng phủ của cùng external Wi-Fi AP; external AP đang phát 5 GHz; laptop đã kết nối vào cùng SSID/subnet; có Ethernet/console backup trước mọi thao tác có thể làm mất Wi-Fi DUT; iperf3 có trên laptop và Jetson.

## TC-JET-STA-5G-007 — Validate WPA2/WPA3 of the connected infrastructure Wi-Fi

Source: `wifi_vd_tcs.ods` · AUTO

**Critical:** Runtime security → WPA2/WPA3; Insecure modes → none; Invalid credential → rejected; Valid reconnect → successful

**Supporting:** None

**Informational/context:** Interface; State; Address; RSSI

**Hard dependencies:** Shared SSH; Control interface; Wi-Fi interface; nmcli; NetworkManager; iw; Self-disruption safety; External 5G STA SSID; External STA credential; Security requirement

**Optional dependencies:** AP profile; Wi-Fi PHY; Target band capability; Current Wi-Fi mode; Current SSID; Current band; Valid network path; Full Wi-Fi qualification path; Required Wi-Fi association; Laptop Wi-Fi interface; Jetson AP IP; iperf3 local; iperf3 Jetson; Backup Ethernet; Auto reconnect; Position confirmed >=1 m; Recorded >=1 m distance; Position confirmed 10 m; Recorded 10 m distance; Valid Wi-Fi credential; Max packet loss (%); Max UDP loss (%); Max UDP jitter (ms); Min UDP receiver (Mbps)

**Source setup (tester verification):** Jetson và Laptop Test đều nằm trong vùng phủ của cùng external Wi-Fi AP; external AP đang phát 5 GHz; laptop đã kết nối vào cùng SSID/subnet; có Ethernet/console backup trước mọi thao tác có thể làm mất Wi-Fi DUT; iperf3 có trên laptop và Jetson.

## TC-JET-STA-5G-008 — SSH + Wi-Fi link stability endurance

Source: `wifi_vd_tcs.ods` · AUTO

**Critical:** Endurance duration → >= 1800 s (source minimum 30 minutes; 2 hours optional); Prolonged Wi-Fi loss → none; SSH drops → none; IP/route → remains valid; Driver/NM resets → none; Manual recovery → none

**Supporting:** None

**Informational/context:** Elapsed; Min RSSI; Errors

**Hard dependencies:** Shared SSH; Control interface; Wi-Fi interface; nmcli; NetworkManager; iw; Valid network path; Full Wi-Fi qualification path; Laptop Wi-Fi interface; Jetson AP IP; External 5G STA SSID

**Optional dependencies:** AP profile; Wi-Fi PHY; Target band capability; Current Wi-Fi mode; Current SSID; Current band; Required Wi-Fi association; iperf3 local; iperf3 Jetson; Backup Ethernet; Auto reconnect; Self-disruption safety; Position confirmed >=1 m; Recorded >=1 m distance; Position confirmed 10 m; Recorded 10 m distance; Valid Wi-Fi credential; External STA credential; Max packet loss (%); Max UDP loss (%); Max UDP jitter (ms); Min UDP receiver (Mbps); Security requirement

**Source setup (tester verification):** Jetson và Laptop Test đều nằm trong vùng phủ của cùng external Wi-Fi AP; external AP đang phát 5 GHz; laptop đã kết nối vào cùng SSID/subnet; có Ethernet/console backup trước mọi thao tác có thể làm mất Wi-Fi DUT; iperf3 có trên laptop và Jetson.
