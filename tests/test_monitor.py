import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest

from device_sre.analysis import compare
from device_sre.demo import payload, run_demo
from device_sre.metrics import normalize, PayloadError, as_time, boundary, iso
from device_sre.monitor import poll, worker_lock
from device_sre.mst import MSTClient, FetchError
from device_sre.settings import Settings
from device_sre.store import Store
from device_sre.tools import Tools

NOW = datetime(2026, 10, 22, 5, 0, tzinfo=timezone.utc)
SERIAL = "000001"


@pytest.fixture
def settings(tmp_path):
    return Settings(devices=(SERIAL,), data_dir=tmp_path)


def snap(now=NOW, **kwargs):
    return normalize(payload(SERIAL, now, **kwargs), SERIAL, now)


def test_actual_mst_wrapper_extended_json_and_data_minimization():
    result = snap()
    assert result["metrics"]["ram_used_percent"] == 45
    assert result["device_id"] == "000001"
    assert result["metrics"]["wifi_signal_raw"] == -55
    assert "wifi_signal_dbm" not in result["metrics"]  # Units must be verified by owner.
    encoded = json.dumps(result)
    assert "latitude" not in encoded and "app_data" not in encoded


@pytest.mark.parametrize("body", [{}, {"data": None}, {"data": {}}, {"TerminalDataResponse": {}}])
def test_empty_200_is_not_a_snapshot(body):
    with pytest.raises(PayloadError, match="empty_device_details"):
        normalize(body, SERIAL, NOW)


def test_identity_missing_timestamp_invalid_values():
    body = payload(SERIAL, NOW)
    with pytest.raises(PayloadError, match="identity_mismatch"):
        normalize(body, "OTHER", NOW)
    details = body["data"]["device_details"]
    details["timestamp"] = details["last_updated"] = None
    with pytest.raises(PayloadError, match="source_timestamp"):
        normalize(body, SERIAL, NOW)
    body = payload(SERIAL, NOW)
    body["data"]["device_details"]["device_metrics"]["ram"]["totalbytes"] = 0
    result = normalize(body, SERIAL, NOW)
    assert "ram_used_percent" not in result["metrics"]
    assert "missing_or_invalid_ram" in result["data_quality"]


def test_source_time_parsing_and_india_date_boundary():
    assert as_time({"$date": {"$numberLong": str(int(NOW.timestamp() * 1000))}}) == NOW
    assert as_time("2026-10-22T05:00:00") is None
    assert iso(boundary("2026-10-22")) == "2026-10-21T18:30:00.000000Z"
    with pytest.raises(ValueError):
        boundary("not-a-date")


def test_first_sample_and_real_interval(settings):
    first = snap(NOW - timedelta(minutes=15))
    assert compare(None, first, settings, NOW)["baseline_status"] == "baseline_created"
    result = compare(first, snap(ram=92), settings, NOW)
    assert result["actual_interval_seconds"] == 900
    assert result["deltas"]["ram_used_percent"]["delta"] == 47
    assert result["deltas"]["ram_used_percent"]["unit"] == "percentage_points"
    assert any(f["code"] == "ram_used_percent_threshold" for f in result["findings"])
    result = compare(first, snap(NOW + timedelta(hours=1)), settings, NOW + timedelta(hours=1))
    assert any(f["code"] == "comparison_gap" for f in result["findings"])


def test_repeated_source_timestamp_stale_future_and_out_of_order(settings):
    first = snap()
    repeated = {**first, "observed_at": iso(NOW + timedelta(minutes=15))}
    result = compare(first, repeated, settings, NOW + timedelta(minutes=15))
    assert result["baseline_status"] == "no_new_telemetry" and not result["deltas"]
    changed = {**snap(ram=99), "observed_at": repeated["observed_at"]}
    result = compare(first, changed, settings, NOW + timedelta(minutes=15))
    assert result["baseline_status"] == "source_timestamp_reused_with_changed_metrics" and not result["deltas"]
    assert compare(first, snap(NOW - timedelta(seconds=1)), settings, NOW)["baseline_status"] == "out_of_order_telemetry"
    result = compare(None, snap(NOW - timedelta(days=1), ram=99), settings, NOW)
    assert result["source_freshness"] == "stale"
    assert not any(f["code"] == "ram_used_percent_threshold" for f in result["findings"])
    result = compare(None, snap(NOW + timedelta(hours=1)), settings, NOW)
    assert result["source_freshness"] == "future_clock"


def test_capacity_change_is_not_comparable_and_raw_codes_are_not_incidents(settings):
    previous = snap(NOW - timedelta(minutes=15))
    current = snap(ram=95)
    current["metrics"]["ram_total_bytes"] *= 2
    current["metrics"]["printer_error_raw"] = 4
    result = compare(previous, current, settings, NOW)
    assert "ram_used_percent" not in result["deltas"]
    assert not any(f["code"] == "printer_error" for f in result["findings"])


def test_network_threshold_requires_verified_units_and_active_interface(settings):
    raw = payload(SERIAL, NOW, wifi=-90)
    default = normalize(raw, SERIAL, NOW)
    assert not any("wifi" in f["code"] for f in compare(None, default, settings, NOW)["findings"])
    verified = normalize(raw, SERIAL, NOW, {"wifi": "dBm", "cellular": None})
    assert any("wifi" in f["code"] for f in compare(None, verified, settings, NOW)["findings"])
    verified["metrics"]["connection_mode"] = 1
    assert not any("wifi" in f["code"] for f in compare(None, verified, settings, NOW)["findings"])


class FixtureClient:
    def __init__(self, now=NOW, fail=False, ram=45):
        self.now, self.fail, self.ram = now, fail, ram

    async def fetch(self, serial, clock):
        if self.fail:
            raise FetchError("mst_http_401")
        return normalize(payload(serial, self.now, ram=self.ram), serial, clock())


def test_failed_poll_preserves_baseline_and_does_not_report_healthy(settings):
    store = Store(settings.data_dir)
    asyncio.run(poll(settings, store, FixtureClient(), lambda: NOW))
    later = NOW + timedelta(minutes=15)
    summary = asyncio.run(poll(settings, store, FixtureClient(fail=True), lambda: later))
    assert summary["failed"] == 1
    assert store.latest(SERIAL)["observed_at"] == iso(NOW)
    fleet = Tools(settings, store, lambda: later).get_fleet_summary()
    assert fleet["coverage"]["unknown"] == 1
    assert fleet["coverage"]["ok"] == 0
    assert fleet["devices"][0]["status"] == "fetch_failed"


def test_changed_reused_source_and_late_telemetry_do_not_replace_baseline(settings):
    store = Store(settings.data_dir)
    asyncio.run(poll(settings, store, FixtureClient(), lambda: NOW))
    asyncio.run(poll(settings, store, FixtureClient(ram=90), lambda: NOW + timedelta(minutes=15)))
    assert store.latest(SERIAL, baseline=True)["metrics"]["ram_used_percent"] == 45
    asyncio.run(poll(settings, store, FixtureClient(NOW - timedelta(seconds=1)), lambda: NOW + timedelta(minutes=30)))
    assert store.latest(SERIAL, baseline=True)["source_timestamp"] == iso(NOW)


def test_partial_fleet_failure(settings):
    settings = replace(settings, devices=(SERIAL, "BAD"))

    class PartialClient(FixtureClient):
        async def fetch(self, serial, clock):
            if serial == "BAD":
                raise FetchError("empty_device_details")
            return await super().fetch(serial, clock)

    summary = asyncio.run(poll(settings, Store(settings.data_dir), PartialClient(), lambda: NOW))
    assert summary["succeeded"] == summary["failed"] == 1


def test_date_range_pagination_restart_and_expiry(settings):
    store = Store(settings.data_dir)
    for minutes in (0, 15, 30):
        observed = NOW + timedelta(minutes=minutes)
        asyncio.run(poll(settings, store, FixtureClient(observed), lambda: observed))
    reader = Tools(settings, Store(settings.data_dir), lambda: NOW + timedelta(minutes=30))
    page = reader.get_device_history(SERIAL, "2026-10-22", "2026-10-23", limit=2)["devices"][0]
    assert len(page["snapshots"]) == 2 and page["has_more"]
    second = reader.get_device_history(SERIAL, "2026-10-22", page["next_end_exclusive"], limit=2)["devices"][0]
    assert len(second["snapshots"]) == 1
    comparison = reader.compare_device_snapshots(SERIAL, iso(NOW), iso(NOW + timedelta(minutes=30)))["devices"][0]
    assert comparison["actual_interval_seconds"] == 1800 and comparison["historical"]
    store.prune(NOW + timedelta(days=8), settings.retention_days)
    assert store.latest(SERIAL) is None


def test_runtime_device_selection_and_worker_exclusion(settings):
    reader = Tools(settings, Store(settings.data_dir))
    result = asyncio.run(reader.get_device_details("ANOTHER-DEVICE", refresh=False))
    assert result["devices"][0]["status"] == "awaiting_first_poll"
    with pytest.raises(ValueError, match="serial"):
        asyncio.run(reader.get_device_details("invalid\nserial", refresh=False))
    with pytest.raises(ValueError):
        reader.list_monitored_devices(limit=1000)
    with worker_lock(settings.data_dir), pytest.raises(ValueError, match="already owns"):
        with worker_lock(settings.data_dir):
            pass


def test_http_contract_retry_credentials_and_no_redirects(settings, monkeypatch):
    monkeypatch.setenv("MST_BEARER_TOKEN", "fixture-only-not-a-real-credential")
    monkeypatch.delenv("MST_TOKEN_FILE", raising=False)
    seen = []

    async def no_wait(_):
        pass

    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(503)
        assert request.url.path == "/v1/mst-sync/analytics/device-details"
        assert request.headers["hardware-serial-number"] == SERIAL
        assert request.headers["authorization"].startswith("Bearer ")
        assert not request.url.query
        return httpx.Response(200, json=payload(SERIAL, NOW))

    result = asyncio.run(MSTClient(settings, httpx.MockTransport(handler), no_wait).fetch(SERIAL, lambda: NOW))
    assert result["device_id"] == SERIAL and len(seen) == 2
    assert seen[0].headers["x-correlation-id"] == seen[1].headers["x-correlation-id"]
    for status in (401, 403, 302):
        requests = []

        def rejected(request):
            requests.append(request)
            return httpx.Response(status, headers={"Location": "https://example.invalid"}, text="sensitive upstream body")

        with pytest.raises(FetchError, match=f"mst_http_{status}"):
            asyncio.run(MSTClient(settings, httpx.MockTransport(rejected), no_wait).fetch(SERIAL, lambda: NOW))
        assert len(requests) == 1


def test_rotated_file_credential_and_empty_200(settings, monkeypatch, tmp_path):
    token = tmp_path / "token"
    token.write_text("first-fixture-token")
    monkeypatch.setenv("MST_TOKEN_FILE", str(token))
    seen = []

    def handler(request):
        seen.append(request.headers["authorization"])
        return httpx.Response(200, json=payload(SERIAL, NOW))

    client = MSTClient(settings, httpx.MockTransport(handler))
    asyncio.run(client.fetch(SERIAL, lambda: NOW))
    token.write_text("second-fixture-token")
    asyncio.run(client.fetch(SERIAL, lambda: NOW))
    assert seen == ["Bearer first-fixture-token", "Bearer second-fixture-token"]
    with pytest.raises(FetchError, match="empty_device_details"):
        asyncio.run(MSTClient(settings, httpx.MockTransport(lambda r: httpx.Response(200, json={"data": None}))).fetch(SERIAL, lambda: NOW))


@pytest.mark.parametrize("url", ["http://mst.invalid", "https://user:secret@mst.invalid", "https://mst.invalid/a", "https://mst.invalid?token=x"])
def test_mst_origin_validation(url):
    with pytest.raises(ValueError):
        Settings(base_url=url)


def test_no_devices_fails_before_worker_start(tmp_path):
    settings = Settings(data_dir=tmp_path)
    with pytest.raises(ValueError, match="serial"):
        asyncio.run(poll(settings, Store(tmp_path)))


def test_demo_repeated_runs_are_isolated(settings):
    first = asyncio.run(run_demo(settings))
    second = asyncio.run(run_demo(settings))
    assert first["data_directory"] != second["data_directory"]
    for result in (first, second):
        evaluation = result["summary"]["devices"][0]["evaluation"]
        assert evaluation["actual_interval_seconds"] == evaluation["source_interval_seconds"] == 900
        assert evaluation["deltas"]["ram_used_percent"]["delta"] == 47

