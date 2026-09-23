"""Semantic presentation metadata for every AUTO Wi-Fi test console.

The collector owns facts and the evaluator owns acceptance.  This module only
decides which of those facts are useful to show for a test.  Keeping that
choice out of the Qt widgets prevents a console from becoming a collection of
per-test UI branches.
"""
from __future__ import annotations

from dataclasses import dataclass

from .catalog import WifiTestCase


@dataclass(frozen=True)
class PresentationMetric:
    label: str
    sources: tuple[str, ...]

    def resolve(self, measurements: dict):
        for source in self.sources:
            value = measurements.get(source)
            if value not in (None, "", "—", "Unknown"):
                return source, value
        return None, None


@dataclass(frozen=True)
class TestPresentation:
    name: str
    summary_metrics: tuple[PresentationMetric, ...]
    supporting_metrics: tuple[PresentationMetric, ...]
    measurement_groups: tuple[tuple[str, tuple[PresentationMetric, ...]], ...]
    setup_requirements: tuple[str, ...] = ()

    @property
    def measurement_metrics(self) -> tuple[PresentationMetric, ...]:
        return tuple(metric for _group, metrics in self.measurement_groups for metric in metrics)


def metric(label: str, *sources: str) -> PresentationMetric:
    return PresentationMetric(label, tuple(sources) or (label,))


RF_SUPPORT = (
    metric("Channel"), metric("Frequency"), metric("Channel width", "Width", "Channel width"),
    metric("BSSID"), metric("Driver"), metric("PHY"),
)
ROUTE_SUPPORT = (
    metric("Route device", "Forward route dev", "Route interface"),
    metric("Route source", "Forward route src"), metric("Gateway", "Forward route via", "Gateway"),
)


PRESENTATIONS: dict[str, TestPresentation] = {
    "ap_runtime": TestPresentation(
        "AP runtime",
        (metric("Interface"), metric("Mode"), metric("Band"), metric("SSID")),
        RF_SUPPORT,
        (("Runtime", (metric("Interface"), metric("Driver"), metric("PHY"), metric("BSSID"),
                      metric("SSID"), metric("Mode"), metric("Band"), metric("Channel"),
                      metric("Frequency"), metric("Channel width", "Width", "Channel width"),
                      metric("AP IPv4"), metric("Interface state"), metric("Regulatory errors"))),),
    ),
    "phy": TestPresentation(
        "PHY",
        (metric("Interface"), metric("PHY capability", "PHY standard capability", "PHY capability"),
         metric("Runtime PHY", "Runtime PHY standard"), metric("Band")),
        (metric("Driver"), metric("PHY"), metric("HE capability"), metric("Runtime HE")),
        (("PHY", (metric("Interface"), metric("Driver"), metric("PHY"),
                  metric("PHY standard capability"), metric("Runtime PHY standard"),
                  metric("HE capability"), metric("Runtime HE"), metric("PHY bitrate collected"))),),
    ),
    "association": TestPresentation(
        "Association",
        (metric("SSID", "Client SSID", "SSID"), metric("Band"),
         metric("Wi-Fi IPv4", "Client IPv4", "DUT Wi-Fi IP", "AP IPv4"), metric("Gateway")),
        RF_SUPPORT + ROUTE_SUPPORT,
        (("Association", (metric("SSID", "Client SSID", "SSID"), metric("BSSID"), metric("Band"),
                          metric("Channel"), metric("Frequency"), metric("Wi-Fi IPv4", "Client IPv4", "DUT Wi-Fi IP", "AP IPv4"),
                          metric("Gateway"), metric("Route interface"), metric("Wi-Fi route"),
                          metric("Association", "Associated"), metric("Reachability", "Ping reachable"))),),
    ),
    "rssi": TestPresentation(
        "RSSI / PHY",
        (metric("Interface"), metric("RSSI"), metric("TX bitrate"), metric("RX bitrate")),
        RF_SUPPORT + (metric("Disconnects"), metric("Reconnects")),
        (("RF samples", (metric("RF samples"), metric("RSSI min"), metric("RSSI average", "RSSI average", "RSSI"),
                         metric("RSSI max"), metric("TX bitrate"), metric("RX bitrate"),
                         metric("Disconnects"), metric("Reconnects"))),),
    ),
    "latency": TestPresentation(
        "Latency",
        (metric("Target", "DUT AP IP", "DUT Wi-Fi IP", "AP IPv4"),
         metric("Wi-Fi route", "Route interface", "Forward Wi-Fi path", "Wi-Fi route"),
         metric("Average RTT", "RTT avg", "Average RTT"), metric("Packet loss")),
        (metric("Disconnects"), metric("RSSI")) + ROUTE_SUPPORT,
        (("Packets", (metric("Packets sent"), metric("Packets received"), metric("Packet loss"))),
         ("RTT", (metric("RTT min"), metric("RTT avg"), metric("RTT max"), metric("RTT mdev")))),
    ),
    "throughput": TestPresentation(
        "Throughput",
        (metric("Target", "DUT AP IP", "DUT Wi-Fi IP", "AP IPv4"), metric("Protocol"),
         metric("Sender Mbps", "Forward sender Mbps", "UDP 5 sender Mbps"),
         metric("Receiver Mbps", "Forward receiver Mbps", "Reverse receiver Mbps", "UDP 5 receiver Mbps")),
        (metric("RSSI"), metric("Disconnects"), metric("Network errors")) + ROUTE_SUPPORT,
        (("TCP", (metric("Forward receiver Mbps"), metric("Reverse receiver Mbps"), metric("Retransmits"))),
         ("UDP", (metric("UDP 5 sender Mbps"), metric("UDP 5 receiver Mbps"), metric("UDP loss"),
                  metric("UDP jitter"), metric("UDP 10 sender Mbps"), metric("UDP 10 receiver Mbps"),
                  metric("UDP 10 loss"), metric("UDP 10 jitter")))),
    ),
    "ssh": TestPresentation(
        "SSH",
        (metric("Target IP", "DUT Wi-Fi IP", "DUT AP IP", "AP IPv4"),
         metric("Wi-Fi route", "Route interface"), metric("TCP/22", "TCP22 reachable"),
         metric("SSH status", "SSH authentication")),
        ROUTE_SUPPORT,
        (("SSH", (metric("Target IP", "DUT Wi-Fi IP", "DUT AP IP", "AP IPv4"),
                  metric("Route interface"), metric("TCP/22", "TCP22 reachable"),
                  metric("Authentication", "SSH authentication"),
                  metric("Remote command", "Remote command execution"),
                  metric("Host identity", "Actual host", "Host identity valid"))),),
    ),
    "security": TestPresentation(
        "Security",
        (metric("SSID"), metric("Security", "Runtime security", "Security"),
         metric("Key management"), metric("PMF")),
        RF_SUPPORT + (metric("SAE capability"), metric("Insecure modes")),
        (("Security", (metric("SSID"), metric("Security", "Runtime security", "Security"),
                       metric("Key management"), metric("PMF"), metric("WPA2 runtime"),
                       metric("SAE capability"), metric("WPA3 connected"), metric("Insecure modes"),
                       metric("Invalid credential rejected"), metric("Valid credential connects"))),),
    ),
    "recovery": TestPresentation(
        "Recovery",
        (metric("Original state", "Mode"), metric("Recovery method"),
         metric("Cycle progress", "Completed cycles", "Reconnect successes"),
         metric("Restore status", "Services recovered", "Valid profile restored", "AP recovered")),
        (metric("Stale routes"), metric("Reboot required"), metric("Disconnects"), metric("Reconnects")),
        (("Recovery", (metric("Cycle count", "Total cycles"), metric("Successful recovery cycles", "Reconnect successes"),
                       metric("Reconnect time"), metric("DHCP recovery time"), metric("Valid profile restored"),
                       metric("AP recovered"), metric("Services recovered"), metric("Stale routes"),
                       metric("Reboot required"))),),
    ),
    "endurance": TestPresentation(
        "Endurance",
        (metric("Interface"), metric("Elapsed", "Elapsed seconds"), metric("Disconnects"), metric("SSH drops")),
        (metric("Reconnects"), metric("RSSI"), metric("Driver resets"), metric("NetworkManager failures")),
        (("Stability", (metric("Elapsed seconds"), metric("Disconnects"), metric("Reconnects"),
                        metric("SSH drops"), metric("Manual recoveries"), metric("Driver resets"),
                        metric("NetworkManager failures"), metric("Service failures"))),),
    ),
}


# Test definitions select a reusable semantic adapter; Qt never branches on a
# concrete test ID.  Bands share the same entry by role and source row number.
_AP_PROFILE = {
    1: "ap_runtime", 2: "phy", 3: "association", 4: "rssi", 5: "latency",
    6: "throughput", 7: "throughput", 8: "association", 9: "throughput",
    10: "security", 11: "recovery", 12: "recovery", 13: "endurance",
}
_STA_PROFILE = {
    1: "association", 2: "ssh", 3: "rssi", 4: "phy", 5: "latency",
    6: "throughput", 7: "security", 8: "endurance",
}


def presentation_for(case: WifiTestCase) -> TestPresentation | None:
    if case.suite != "AUTO":
        return None
    number = int(case.test_id.rsplit("-", 1)[1])
    profile = (_AP_PROFILE if case.wifi_role == "AP" else _STA_PROFILE)[number]
    setup = ()
    if case.wifi_role == "AP" and number == 8:
        setup = ("distance_1m_confirmed",)
    elif case.wifi_role == "AP" and number == 9:
        setup = ("distance_10m_confirmed",)
    base = PRESENTATIONS[profile]
    return TestPresentation(base.name, base.summary_metrics, base.supporting_metrics,
                            base.measurement_groups, setup)


def resolved_metrics(specs: tuple[PresentationMetric, ...], measurements: dict) -> list[tuple[str, str, object]]:
    rows = []
    seen = set()
    for spec in specs:
        source, value = spec.resolve(measurements)
        if source is not None and spec.label not in seen:
            rows.append((spec.label, source, value))
            seen.add(spec.label)
    return rows
