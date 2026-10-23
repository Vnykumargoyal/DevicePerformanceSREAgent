import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from device_sre.analysis import compare
from device_sre.metrics import as_time, normalize, PayloadError, iso
from device_sre.monitor import poll
from device_sre.mst import MSTClient
from device_sre.settings import Settings, DEFAULT_THRESHOLDS
from device_sre.server import create_server
from device_sre.store import Store

SERIAL = "FIXTURE-DEVICE"
SOURCE = datetime(2026, 9, 21, 7, 54, 0, 538000, tzinfo=timezone.utc)
TODAY = datetime(2026, 10, 22, 6, 0, tzinfo=timezone.utc)


@pytest.fixture
def response():
    return json.loads((Path(__file__).parent / "fixtures" / "device_response.json").read_text())


def test_supplied_schema_extracts_cpu_battery_states_and_derived_capacity(response):
    snapshot = normalize(response, SERIAL, TODAY)
    metrics = snapshot["metrics"]
    assert metrics["ram_used_percent"] == round(100 * 762195968 / 1984270336, 3)
    assert metrics["storage_used_percent"] == round(100 * 2391355392 / 13058928640, 3)
    assert metrics["cpu_used_percent"] == 18.818
    assert metrics["ram_available_bytes"] == 1222074368
    assert metrics["storage_available_bytes"] == 10667573248
    assert metrics["ram_reported_available_bytes"] == metrics["storage_reported_available_bytes"] == 0
    assert "ram_available_bytes_mismatch" in snapshot["data_quality"]
    assert "storage_available_bytes_mismatch" in snapshot["data_quality"]
    assert metrics["battery_level_raw"] == 9 and metrics["battery_is_charging"] is True
    assert metrics["cpu_temperature_raw"] == 42.99
    assert metrics["wifi_signal_raw"] == -52 and metrics["cellular_signal_raw"] == 4
    assert "cellular_signal_dbm" not in metrics
    assert metrics["wifi_connected"] is True and metrics["cellular_data_enabled"] is False
    assert metrics["mqtt_connected_count"] == 2
    assert metrics["card_tap_error_raw"] == -1
    assert snapshot["software"]["device_info"]["os_version"] == "8.1.0"
    assert snapshot["software"]["app_versions"]["com.pinelabs.plutusplus"] == "080926"


def test_old_supplied_snapshot_is_historical_and_not_a_trend(response):
    snapshot = normalize(response, SERIAL, TODAY)
    result = compare(None, snapshot, Settings(), TODAY)
    assert result["source_freshness"] == "stale"
    assert result["baseline_status"] == "baseline_created"
    assert not result["deltas"] and not result["reported_transitions"]
    assert result["software_changes"]["apps_status"] == "stale_or_future_app_data"
    codes = {f["code"] for f in result["findings"]}
    assert "telemetry_stale" in codes and "api_reports_offline" in codes
    assert not any(code.endswith("_threshold") for code in codes)


def test_private_identifiers_and_unreliable_dates_never_reach_normalized_output(response):
    details, app = response["data"]["device_details"], response["data"]["app_data"]
    details["_id"] = {"timestamp": 1762333090, "date": "2025-11-05T08:58:10Z"}
    details["location_metrics"] = {"coordinates": {"latitude": 12, "longitude": 34}}
    details["wifi_metrics"].update(ssid="PRIVATE_SSID", ipaddress="192.0.2.1", macaddress="SECRET_MAC")
    details["device_metrics"]["mqttstatus"][0]["name"] = "SECRET_TOPIC"
    details["bluetooth_metrics"] = {"connecteddevices": [{"name": "PERSONAL_PHONE"}]}
    app["app_data"][0].update(permissions="CAMERA", last_update_datetime="1970-01-01T00:00:00Z")
    snapshot = normalize(response, SERIAL, TODAY)
    encoded = json.dumps(snapshot)
    for forbidden in ("PRIVATE_SSID", "192.0.2.1", "SECRET_MAC", "SECRET_TOPIC", "PERSONAL_PHONE", "latitude", "1970-01-01", "CAMERA"):
        assert forbidden not in encoded
    assert as_time(snapshot["source_timestamp"]) == SOURCE


def test_app_identity_mismatch_is_rejected(response):
    response["data"]["app_data"]["device_id"] = "OTHER_DEVICE"
    with pytest.raises(PayloadError, match="app_device_identity_mismatch"):
        normalize(response, SERIAL, TODAY)


def test_two_distinct_readings_compare_cpu_states_and_app_versions(response):
    first = normalize(response, SERIAL, SOURCE)
    later = SOURCE + timedelta(minutes=15)
    response["data"]["device_details"]["timestamp"] = iso(later)
    response["data"]["device_details"]["device_metrics"]["cpu"]["usedpercentage"] = 91
    response["data"]["device_details"]["device_metrics"]["battery"]["ischarging"] = False
    response["data"]["device_details"]["wifi_metrics"]["connected"] = False
    response["data"]["app_data"]["timestamp"] = iso(later)
    response["data"]["app_data"]["app_data"][6]["app_version"] = "NEW-FIXTURE-VERSION"
    second = normalize(response, SERIAL, later)
    result = compare(first, second, Settings(), later)
    assert result["deltas"]["cpu_used_percent"]["delta"] == 72.182
    assert result["deltas"]["cpu_used_percent"]["unit"] == "percentage_points"
    assert result["reported_transitions"]["battery_is_charging"]["current"] is False
    assert result["reported_transitions"]["wifi_connected"]["previous"] is True
    assert result["software_changes"]["apps"][0]["previous"] == "080926"
    assert result["software_changes"]["apps"][0]["current"] == "NEW-FIXTURE-VERSION"
    assert any(f["code"] == "cpu_used_percent_threshold" for f in result["findings"])


def test_raw_codes_and_network_counters_never_become_incident_deltas(response):
    first = normalize(response, SERIAL, SOURCE)
    later = SOURCE + timedelta(minutes=15)
    response["data"]["device_details"]["timestamp"] = iso(later)
    response["data"]["device_details"]["device_metrics"]["printererror"] = 4
    response["data"]["device_details"]["wifi_metrics"]["networkstats"]["latency"] = 3
    result = compare(first, normalize(response, SERIAL, later), Settings(), later)
    assert "printer_error_raw" not in result["deltas"]
    assert "wifi_latency_raw" not in result["deltas"]
    assert "printer_error_raw" in result["reported_transitions"]
    assert not any(f["code"] == "printer_error" for f in result["findings"])


def test_app_inventory_uses_its_own_source_clock(response):
    first = normalize(response, SERIAL, SOURCE)
    response["data"]["app_data"]["app_data"][6]["app_version"] = "DIFFERENT"
    same_time = normalize(response, SERIAL, SOURCE + timedelta(minutes=1))
    result = compare(first, same_time, Settings(), SOURCE + timedelta(minutes=1))
    assert result["baseline_status"] == "no_new_telemetry"
    assert result["software_changes"]["apps_status"] == "app_timestamp_reused_with_changed_inventory"
    assert result["software_changes"]["apps"] == []


def test_adapter_upgrade_is_not_reported_as_reused_timestamp_corruption(response, tmp_path):
    current = normalize(response, SERIAL, SOURCE)
    old = deepcopy(current)
    old.pop("normalization_version")
    old["metrics"] = {"ram_used_percent": current["metrics"]["ram_used_percent"]}

    class Client:
        async def fetch(self, serial, clock):
            return deepcopy(current if clock() > SOURCE else old)

    store = Store(tmp_path)
    asyncio.run(poll(Settings(), store, client=Client(), clock=lambda: SOURCE, serial_numbers=SERIAL))
    later = SOURCE + timedelta(minutes=1)
    current["observed_at"] = iso(later)
    result = asyncio.run(poll(Settings(), store, client=Client(), clock=lambda: later, serial_numbers=SERIAL))
    evaluation = result["devices"][0]["evaluation"]
    assert evaluation["baseline_status"] == "normalization_version_changed"
    assert not evaluation["deltas"]
    assert "source_timestamp_reused_with_changed_metrics" not in store.latest(SERIAL)["data_quality"]


def test_existing_threshold_config_gets_new_cpu_default(tmp_path, monkeypatch):
    legacy = {key: value for key, value in DEFAULT_THRESHOLDS.items() if key != "cpu_used_percent"}
    config = tmp_path / "monitor.json"
    config.write_text(json.dumps({"thresholds": legacy}))
    monkeypatch.setenv("SRE_CONFIG", str(config))
    assert Settings.load().thresholds["cpu_used_percent"] == 85


def test_real_response_shape_flows_through_mst_adapter(response, tmp_path, monkeypatch):
    monkeypatch.delenv("MST_TOKEN_FILE", raising=False)
    monkeypatch.setenv("MST_BEARER_TOKEN", "fixture-only-token")
    settings = Settings(data_dir=tmp_path)
    client = MSTClient(settings, transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response)))
    snapshot = asyncio.run(client.fetch(SERIAL, lambda: TODAY))
    assert snapshot["metrics"]["cpu_used_percent"] == 18.818
    assert snapshot["software"]["app_versions"]["com.pinelabs.plutusplus"] == "080926"


@pytest.mark.parametrize("selection", [SERIAL, [SERIAL, "DENIED", SERIAL]])
def test_supplied_response_through_mcp_fetch_cache_and_history(response, tmp_path, monkeypatch, selection):
    monkeypatch.delenv("MST_TOKEN_FILE", raising=False)
    monkeypatch.setenv("MST_BEARER_TOKEN", "fixture-only-token")
    requests = []

    def respond(request):
        serial = request.headers["hardware-serial-number"]
        requests.append(serial)
        assert request.url.path == "/v1/mst-sync/analytics/device-details"
        assert request.headers["authorization"] == "Bearer fixture-only-token"
        return httpx.Response(403) if serial == "DENIED" else httpx.Response(200, json=response)

    settings = Settings(data_dir=tmp_path)
    client = MSTClient(settings, transport=httpx.MockTransport(respond))
    server = create_server(settings, client=client, clock=lambda: TODAY)

    async def check():
        async with create_connected_server_and_client_session(server._mcp_server) as session:
            result = await session.call_tool("get_device_details", {"serial_numbers": selection})
            assert not result.isError
            batch = json.loads(result.content[0].text)
            assert batch["succeeded"] == 1
            assert batch["failed"] == (0 if isinstance(selection, str) else 1)
            assert batch["requested"] == len(set(requests))
            device = batch["devices"][0]
            assert device["snapshot"]["metrics"]["cpu_used_percent"] == 18.818
            assert device["snapshot"]["metrics"]["ram_available_bytes"] == 1222074368
            assert device["snapshot"]["metrics"]["storage_available_bytes"] == 10667573248
            assert device["snapshot"]["metrics"]["battery_is_charging"] is True
            assert device["snapshot"]["software"]["app_versions"]["com.pinelabs.plutusplus"] == "080926"
            assert device["evaluation"]["source_freshness"] == "stale"
            assert device["evaluation"]["baseline_status"] == "baseline_created"
            assert not device["evaluation"]["deltas"]
            if batch["failed"]:
                assert batch["devices"][1]["error_code"] == "mst_http_403"
            count = len(requests)
            cached = await session.call_tool("get_device_details", {"serial_numbers": SERIAL, "refresh": False})
            assert json.loads(cached.content[0].text)["devices"][0]["snapshot"] == device["snapshot"]
            history = await session.call_tool("get_device_history", {
                "serial_numbers": [SERIAL], "start": "2026-10-22", "end": "2026-10-23"})
            assert json.loads(history.content[0].text)["devices"][0]["snapshots"] == [device["snapshot"]]
            assert len(requests) == count

    asyncio.run(check())


@pytest.mark.parametrize("new_source", [True, False])
def test_mcp_compares_new_sample_but_not_a_repeated_response(response, tmp_path, monkeypatch, new_source):
    monkeypatch.delenv("MST_TOKEN_FILE", raising=False)
    monkeypatch.setenv("MST_BEARER_TOKEN", "fixture-only-token")
    current_time = SOURCE
    settings = Settings(data_dir=tmp_path)
    client = MSTClient(settings, transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response)))
    server = create_server(settings, client=client, clock=lambda: current_time)

    async def check():
        nonlocal current_time
        async with create_connected_server_and_client_session(server._mcp_server) as session:
            first = await session.call_tool("get_device_details", {"serial_numbers": SERIAL})
            assert not first.isError
            current_time += timedelta(minutes=15)
            if new_source:
                response["data"]["device_details"]["timestamp"] = iso(current_time)
                response["data"]["device_details"]["device_metrics"]["cpu"]["usedpercentage"] = 91
                response["data"]["app_data"]["timestamp"] = iso(current_time)
                response["data"]["app_data"]["app_data"][6]["app_version"] = "TEST-NEXT-VERSION"
            second = await session.call_tool("get_device_details", {"serial_numbers": [SERIAL]})
            assert not second.isError
            evaluation = json.loads(second.content[0].text)["devices"][0]["evaluation"]
            assert evaluation["actual_interval_seconds"] == 900
            if new_source:
                assert evaluation["baseline_status"] == "compared"
                assert evaluation["deltas"]["cpu_used_percent"]["delta"] == 72.182
                assert evaluation["software_changes"]["apps"][0]["previous"] == "080926"
                assert evaluation["software_changes"]["apps"][0]["current"] == "TEST-NEXT-VERSION"
            else:
                assert evaluation["baseline_status"] == "no_new_telemetry"
                assert evaluation["deltas"] == {}
            exact = await session.call_tool("compare_device_snapshots", {
                "serial_numbers": SERIAL,
                "previous_observed_at": iso(SOURCE), "current_observed_at": iso(current_time)})
            historical = json.loads(exact.content[0].text)["devices"][0]
            assert historical["baseline_status"] == evaluation["baseline_status"]
            assert historical["deltas"] == evaluation["deltas"]

    asyncio.run(check())


@pytest.mark.parametrize("extra_serial", [[], ["UNSEEN"]])
def test_offline_mcp_replay_cli_needs_no_credentials_or_live_cache(tmp_path, monkeypatch, extra_serial):
    root = Path(__file__).parents[1]
    # Deliberately invalid production credential paths must never be read in replay.
    monkeypatch.setenv("MST_TOKEN_FILE", str(tmp_path / "no-mst-token"))
    monkeypatch.setenv("SRE_DATA_DIR", str(tmp_path / "operational-data"))
    monkeypatch.setenv("SRE_CONFIG", str(tmp_path / "no-config.json"))
    completed = subprocess.run([
        sys.executable, str(root / "scripts" / "replay_mcp.py"),
        "--offline", "--file", str(root / "tests" / "fixtures" / "device_response.json"),
        "--serial-numbers", SERIAL, *extra_serial, "--assessed-at", iso(TODAY),
    ], cwd=root, capture_output=True, text=True, timeout=30)
    assert completed.returncode == (2 if extra_serial else 0), completed.stderr
    report = json.loads(completed.stdout)
    assert report["fetched_from_mst"] is False
    assert report["mcp_tool_result"]["fetched_from_mst"] is False
    assert report["mcp_tool_result"]["fetched_from_mst_count"] == 0
    assert report["mcp_tool_result"]["devices"][0]["data_source"] == "replay"
    assert report["mcp_tool_result"]["devices"][0]["fetched_from_mst"] is False
    assert report["operational_cache_modified"] is False
    assert report["temporary_replay_cache_removed"] is True
    assert report["mcp_tool_result"]["succeeded"] == 1
    assert report["mcp_tool_result"]["failed"] == len(extra_serial)
    verification = report["verification"]
    assert verification["one_request_per_unique_serial"]
    assert verification["cache_reads_did_not_fetch"] and verification["cached_snapshots_match"]
    assert verification["history_observations_per_device"][SERIAL] == 1
    if extra_serial:
        missing = report["mcp_tool_result"]["devices"][1]
        assert missing["snapshot"] is None
        assert missing["error_code"] == "replay_sample_missing"
        assert missing["error_details"]["stage"] == "offline_replay"
    assert not (tmp_path / "operational-data").exists()


def test_switching_serial_creates_its_own_mcp_baseline(response, tmp_path, monkeypatch):
    monkeypatch.delenv("MST_TOKEN_FILE", raising=False)
    monkeypatch.setenv("MST_BEARER_TOKEN", "fixture-only-token")
    second_serial = "ANOTHER-DEVICE"
    second_response = deepcopy(response)
    second_response["data"]["device_details"]["device_id"] = second_serial
    second_response["data"]["app_data"]["device_id"] = second_serial
    second_response["data"]["device_details"]["device_metrics"]["cpu"]["usedpercentage"] = 55
    responses = {SERIAL: response, second_serial: second_response}
    requests = []

    def respond(request):
        serial = request.headers["hardware-serial-number"]
        requests.append(serial)
        return httpx.Response(200, json=responses[serial])

    settings = Settings(data_dir=tmp_path)
    client = MSTClient(settings, transport=httpx.MockTransport(respond))
    server = create_server(settings, client=client, clock=lambda: SOURCE)

    async def check():
        async with create_connected_server_and_client_session(server._mcp_server) as session:
            await session.call_tool("get_device_details", {"serial_numbers": SERIAL})
            fresh = await session.call_tool("get_device_details", {"serial_numbers": second_serial})
            item = json.loads(fresh.content[0].text)["devices"][0]
            assert item["snapshot"]["device_id"] == second_serial
            assert item["snapshot"]["metrics"]["cpu_used_percent"] == 55
            assert item["evaluation"]["baseline_status"] == "baseline_created"
            assert item["evaluation"]["previous_observed_at"] is None
            cached = await session.call_tool("get_device_details", {
                "serial_numbers": [SERIAL, second_serial], "refresh": False})
            devices = json.loads(cached.content[0].text)["devices"]
            assert [item["snapshot"]["metrics"]["cpu_used_percent"] for item in devices] == [18.818, 55]

    asyncio.run(check())
    assert requests == [SERIAL, second_serial]


def test_replay_with_no_matching_file_stops_before_mcp(tmp_path, monkeypatch):
    root = Path(__file__).parents[1]
    monkeypatch.setenv("SRE_CONFIG", str(tmp_path / "no-config.json"))
    monkeypatch.setenv("SRE_DATA_DIR", str(tmp_path / "operational-cache"))
    completed = subprocess.run([
        sys.executable, str(root / "scripts" / "replay_mcp.py"),
        "--offline", "--file", str(root / "tests" / "fixtures" / "device_response.json"),
        "--serial-numbers", "UNSEEN",
    ], cwd=root, capture_output=True, text=True, timeout=30)
    assert completed.returncode == 2
    assert not completed.stdout
    assert "Offline replay stopped" in completed.stderr
    assert "Available response serial(s): " + SERIAL in completed.stderr
    assert "scripts/check_mcp.py --transport stdio --serial-numbers UNSEEN --details-only" in completed.stderr
    assert "Processing request" not in completed.stderr
    assert not (tmp_path / "operational-cache").exists()
