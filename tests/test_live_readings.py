"""Online requests must never present stored/offline metrics as current device data."""
import asyncio
from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest

from device_sre.demo import payload
from device_sre.mst import MSTClient
from device_sre.settings import Settings
from device_sre.store import Store
from device_sre.tools import Tools

NOW = datetime(2026, 10, 23, 5, 0, tzinfo=timezone.utc)


def make_tools(tmp_path, monkeypatch, upstream, *, data_source="mst", **settings_options):
    monkeypatch.setenv("MST_BEARER_TOKEN", "synthetic-mst-token")
    monkeypatch.delenv("MST_TOKEN_FILE", raising=False)
    settings = Settings(data_dir=tmp_path, **settings_options)
    client = MSTClient(settings, transport=httpx.MockTransport(upstream), data_source=data_source)
    return Tools(settings, Store(tmp_path), clock=lambda: NOW, client=client)


def test_online_batch_withholds_offline_old_unknown_future_and_denied_metrics(tmp_path, monkeypatch):
    readings = {"0007": (True, 0), "OFFLINE": (False, 0), "OLD": (True, 901),
                "UNKNOWN": (None, 0), "FUTURE": (True, -1)}
    requests = []

    def upstream(request):
        serial = request.headers["hardware-serial-number"]
        requests.append(serial)
        if serial == "DENIED":
            return httpx.Response(403)
        online, age = readings[serial]
        body = payload(serial, NOW - timedelta(seconds=age))
        body["data"]["device_details"]["is_device_online"] = online
        return httpx.Response(200, json=body)

    tools = make_tools(tmp_path, monkeypatch, upstream)

    async def check():
        batch = await tools.get_device_details([*readings, "0007", "DENIED"], live_only=True)
        assert (batch["requested"], batch["succeeded"], batch["failed"]) == (6, 1, 5)
        assert batch["live_only"] is True and batch["live_reading_count"] == 1
        assert batch["live_max_age_seconds"] == 900
        # API provenance remains truthful even for rejected source readings.
        assert batch["fetched_from_mst_count"] == 5 and batch["fetched_from_mst"] is False
        devices = {device["device_id"]: device for device in batch["devices"]}
        assert devices["0007"]["live_reading_available"] is True
        assert devices["0007"]["snapshot"]["metrics"]["ram_used_percent"] == 45
        for serial, code in {"OFFLINE": "live_device_offline", "OLD": "live_telemetry_stale",
                             "UNKNOWN": "live_status_unknown", "FUTURE": "live_telemetry_future",
                             "DENIED": "mst_http_403"}.items():
            device = devices[serial]
            assert device["error_code"] == code
            assert device["live_reading_available"] is False and device["snapshot"] is None
            assert "evaluation" not in device and "ram_used_percent" not in json.dumps(device)
            if serial != "DENIED":
                assert device["fetched_from_mst"] is True
                assert device["source_status"]["max_source_age_seconds"] == 900
        # A second current request must call MST again, regardless of the saved baseline.
        tools.clock = lambda: NOW + timedelta(seconds=1)
        assert (await tools.get_device_details("0007", live_only=True))["succeeded"] == 1
        assert requests.count("0007") == 2 and len(requests) == 7

    asyncio.run(check())


def test_failed_online_refresh_cannot_fall_back_to_previously_cached_metrics(tmp_path, monkeypatch):
    calls = 0

    def upstream(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=payload("0007", NOW)) if calls == 1 else httpx.Response(403)

    tools = make_tools(tmp_path, monkeypatch, upstream)

    async def check():
        assert (await tools.get_device_details("0007", live_only=True))["succeeded"] == 1
        failed = (await tools.get_device_details("0007", live_only=True))["devices"][0]
        assert failed["snapshot"] is None and "evaluation" not in failed
        assert failed["error_code"] == "mst_http_403" and failed["fetched_from_mst"] is False
        assert tools.store.latest("0007") is not None  # History is retained separately.
        with pytest.raises(ValueError, match="requires refresh=true"):
            await tools.get_device_details("0007", refresh=False, live_only=True)
        assert calls == 2

    asyncio.run(check())


@pytest.mark.parametrize("age, expected", [(600, True), (600.001, False)])
def test_live_age_uses_the_stricter_interval_with_an_exact_boundary(tmp_path, monkeypatch, age, expected):
    tools = make_tools(tmp_path, monkeypatch, lambda request: httpx.Response(
        200, json=payload("0007", NOW - timedelta(seconds=age))), stale_seconds=600)
    batch = asyncio.run(tools.get_device_details("0007", live_only=True))
    assert batch["live_max_age_seconds"] == 600
    assert batch["devices"][0]["live_reading_available"] is expected


def test_replayed_online_sample_cannot_satisfy_a_current_mst_request(tmp_path, monkeypatch):
    tools = make_tools(tmp_path, monkeypatch, lambda request: httpx.Response(
        200, json=payload("0007", NOW)), data_source="replay")
    batch = asyncio.run(tools.get_device_details("0007", live_only=True))
    assert batch["succeeded"] == 0 and batch["fetched_from_mst"] is False
    assert batch["devices"][0]["snapshot"] is None
    assert batch["devices"][0]["error_code"] == "live_reading_unavailable"


def test_explicit_full_response_retains_offline_metrics_and_software_with_stale_warnings(tmp_path, monkeypatch):
    source = NOW - timedelta(days=2)
    body = payload("0007", source)
    details = body["data"]["device_details"]
    details["is_device_online"] = False
    details["device_metrics"]["battery"].update(level=16, ischarging=True, temperature=26.4)
    details["device_metrics"]["carddiperror"] = -1
    details["wifi_metrics"]["networkstats"] = {"latency": 74}
    details["device_info"] = {"os_version": "10", "firmware_version": "001.02"}
    body["data"]["app_data"] = {"device_id": "0007", "timestamp": source.isoformat(),
        "app_data": [{"package_name": "com.example.synthetic", "app_version": "001.02"}]}
    tools = make_tools(tmp_path, monkeypatch, lambda request: httpx.Response(200, json=body))
    batch = asyncio.run(tools.get_device_details("0007", refresh=True, live_only=False))
    assert batch["live_only"] is False and batch["succeeded"] == 1
    device = batch["devices"][0]
    assert device["fetched_from_mst"] is True and "live_reading_available" not in device
    snapshot = device["snapshot"]
    assert snapshot["is_device_online"] is False
    assert snapshot["metrics"]["battery_level_raw"] == 16
    assert snapshot["metrics"]["battery_is_charging"] is True
    assert snapshot["metrics"]["card_dip_error_raw"] == -1
    assert snapshot["metrics"]["wifi_latency_raw"] == 74
    assert snapshot["software"]["app_versions"] == {"com.example.synthetic": "001.02"}
    assert device["evaluation"]["source_freshness"] == "stale"
    assert {"api_reports_offline", "telemetry_stale"} <= {finding["code"] for finding in device["evaluation"]["findings"]}
