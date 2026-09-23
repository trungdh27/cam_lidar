"""Regression coverage for semantic, per-TC AUTO console presentation."""
from dataclasses import replace

import pytest

from desktop_app.wifi.auto_suite import evaluate_auto_case
from desktop_app.wifi.catalog import load_auto_catalog
from desktop_app.wifi.presentation import presentation_for, resolved_metrics
from tests.test_wifi_auto_suite import passing_measurements, ready_setup


CASES = {case.test_id: case for case in load_auto_catalog()}


@pytest.mark.parametrize("test_id,expected,excluded", [
    ("TC-JET-5G-001", {"Interface", "Mode", "Band", "SSID"}, {"Average RTT", "TCP/22"}),
    ("TC-JET-5G-005", {"Target", "Wi-Fi route", "Average RTT", "Packet loss"}, {"Mode", "SSID"}),
    ("TC-JET-STA-24G-002", {"Target IP", "Wi-Fi route", "TCP/22", "SSH status"}, {"Mode", "Band"}),
    ("TC-JET-5G-012", {"Original state", "Recovery method", "Cycle progress", "Restore status"}, {"Average RTT", "SSID"}),
])
def test_each_tc_selects_reusable_semantic_summary(test_id, expected, excluded):
    labels = {item.label for item in presentation_for(CASES[test_id]).summary_metrics}
    assert labels == expected
    assert not labels & excluded


def test_measurement_groups_are_tc_specific_and_omit_unrelated_metrics():
    rtt = presentation_for(CASES["TC-JET-5G-005"])
    values = {"Packets sent": 100, "Packets received": 100, "Packet loss": 0,
              "RTT min": 2.1, "RTT avg": 3.2, "RTT max": 5.4, "RTT mdev": .4,
              "SSID": "should-not-leak-into-rtt-table", "PHY": "phy0"}
    rows = {label for label, _source, _value in resolved_metrics(rtt.measurement_metrics, values)}
    assert rows == {"Packets sent", "Packets received", "Packet loss", "RTT min", "RTT avg", "RTT max", "RTT mdev"}


def test_only_distance_tcs_declare_human_setup_metadata():
    assert presentation_for(CASES["TC-JET-5G-001"]).setup_requirements == ()
    assert presentation_for(CASES["TC-JET-5G-008"]).setup_requirements == ("distance_1m_confirmed",)
    assert presentation_for(CASES["TC-JET-5G-009"]).setup_requirements == ("distance_10m_confirmed",)


@pytest.mark.parametrize("channel,frequency", [(36, 5180), (48, 5240), (149, 5745)])
def test_generic_ap_5g_accepts_any_valid_channel_frequency_pair(channel, frequency):
    case = CASES["TC-JET-5G-001"]
    setup = replace(ready_setup(), current_band="5 GHz")
    measurements = passing_measurements(case, setup)
    measurements.update(Channel=channel, Frequency=frequency)
    rows, result, _reason = evaluate_auto_case(case, setup, measurements)
    assert result == "PASS"
    assert next(row for row in rows if row["metric"] == "Channel")["status"] == "PASS"
    assert next(row for row in rows if row["metric"] == "Frequency")["status"] == "PASS"


@pytest.mark.parametrize("channel,frequency", [(1, 2412), (6, 2437), (11, 2462)])
def test_generic_ap_24g_accepts_common_valid_channels(channel, frequency):
    case = CASES["TC-JET-24G-001"]
    setup = replace(ready_setup(), current_band="2.4 GHz")
    measurements = passing_measurements(case, setup)
    measurements.update(Channel=channel, Frequency=frequency)
    assert evaluate_auto_case(case, setup, measurements)[1] == "PASS"


def test_invalid_channel_frequency_relationship_is_supporting_failure_not_fixed_channel_failure():
    case = CASES["TC-JET-5G-001"]
    setup = replace(ready_setup(), current_band="5 GHz")
    measurements = passing_measurements(case, setup)
    measurements.update(Channel=48, Frequency=5180)
    rows, result, _reason = evaluate_auto_case(case, setup, measurements)
    frequency = next(row for row in rows if row["metric"] == "Frequency")
    assert frequency["status"] == "FAIL" and frequency["importance"] == "SUPPORTING"
    assert result == "PASS"
