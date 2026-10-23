import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import sys

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from device_sre.demo import payload
from device_sre.metrics import iso, normalize
from device_sre.monitor import poll
from device_sre.mst import MSTClient
from device_sre.server import create_server
from device_sre.settings import Settings, normalize_serial_numbers
from device_sre.store import Store
from device_sre.tools import Tools

NOW = datetime(2026, 10, 22, 5, 0, tzinfo=timezone.utc)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.delenv("MST_TOKEN_FILE", raising=False)
    monkeypatch.setenv("MST_BEARER_TOKEN", "fixture-only-token")
    settings = Settings(data_dir=tmp_path)  # No serials in configuration.
    requests = []

    def handler(request):
        serial = request.headers["hardware-serial-number"]
        requests.append(serial)
        assert request.headers["authorization"] == "Bearer fixture-only-token"
        if serial == "DENIED":
            return httpx.Response(403)
        if serial == "EMPTY":
            return httpx.Response(200, json={"data": None})
        return httpx.Response(200, json=payload(serial, NOW))

    client = MSTClient(settings, transport=httpx.MockTransport(handler))
    reader = Tools(settings, Store(tmp_path), clock=lambda: NOW, client=client)
    return settings, client, reader, requests


@pytest.mark.parametrize("selection", ["000001", ["000001"]])
def test_single_string_and_one_item_list_fetch_without_static_config(runtime, selection):
    settings, _, reader, requests = runtime
    result = asyncio.run(reader.get_device_details(selection))
    assert settings.devices == ()
    assert requests == ["000001"]
    assert result["requested"] == result["succeeded"] == 1 and result["failed"] == 0
    assert result["fetched_from_mst"] is True and result["fetched_from_mst_count"] == 1
    assert result["data_source"] == "mst"
    assert result["devices"][0]["fetched_from_mst"] is True
    assert result["devices"][0]["status"] == "fetched_observation"
    assert result["devices"][0]["snapshot"]["device_id"] == "000001"
    assert result["devices"][0]["evaluation"]["baseline_status"] == "baseline_created"


def test_mixed_batch_keeps_successes_and_reports_failures_individually(runtime):
    _, _, reader, requests = runtime
    result = asyncio.run(reader.get_device_details(["000001", "DENIED", "000002", "EMPTY", "000001"]))
    assert result["requested"] == 4 and result["succeeded"] == result["failed"] == 2
    assert result["fetched_from_mst"] is False and result["fetched_from_mst_count"] == 2
    assert result["data_source"] == "mixed"
    assert sorted(requests) == ["000001", "000002", "DENIED", "EMPTY"]
    by_id = {item["device_id"]: item for item in result["devices"]}
    assert by_id["000001"]["snapshot"]["device_id"] == "000001"
    assert by_id["000002"]["snapshot"]["device_id"] == "000002"
    assert by_id["DENIED"]["snapshot"] is None and by_id["DENIED"]["error_code"] == "mst_http_403"
    assert by_id["EMPTY"]["error_code"] == "empty_device_details"
    assert by_id["000001"]["fetched_from_mst"] is True
    assert by_id["000002"]["fetched_from_mst"] is True
    assert by_id["DENIED"]["fetched_from_mst"] is False
    assert by_id["EMPTY"]["fetched_from_mst"] is False
    assert reader.list_monitored_devices()["total"] == 4
    assert reader.get_fleet_summary(serial_numbers=["000001", "DENIED"])["coverage"]["total"] == 2


def test_cached_batch_does_not_call_mst(runtime):
    _, _, reader, requests = runtime
    asyncio.run(reader.get_device_details("000001"))
    requests.clear()
    result = asyncio.run(reader.get_device_details(["000001", "UNSEEN"], refresh=False))
    assert requests == []
    assert result["fetched_from_mst"] is False and result["fetched_from_mst_count"] == 0
    assert result["devices"][0]["data_source"] == "cache"
    assert result["devices"][0]["fetched_from_mst"] is False
    assert result["succeeded"] == result["failed"] == 1
    assert result["devices"][1]["error_code"] == "no_cached_observation"


@pytest.mark.parametrize("selection", [None, [], "", 123, ["000001", 123], ["invalid\nserial"],
                                        [f"SN-{index}" for index in range(101)]])
def test_invalid_inputs_reject_entire_request_before_any_network_call(runtime, selection):
    _, _, reader, requests = runtime
    with pytest.raises(ValueError):
        asyncio.run(reader.get_device_details(selection))
    assert not requests


def test_runtime_poll_compares_second_reading_and_history_for_multiple_devices(runtime):
    settings, client, reader, _ = runtime
    serials = ["000001", "000002"]
    earlier = NOW - timedelta(minutes=15)

    def first_handler(request):
        return httpx.Response(200, json=payload(request.headers["hardware-serial-number"], earlier, ram=20))

    first_client = MSTClient(settings, transport=httpx.MockTransport(first_handler))
    asyncio.run(poll(settings, reader.store, client=first_client, clock=lambda: earlier, serial_numbers=serials))
    result = asyncio.run(reader.get_device_details(serials))
    assert all(item["evaluation"]["deltas"]["ram_used_percent"]["delta"] == 25 for item in result["devices"])
    histories = reader.get_device_history(serials, "2026-10-22", "2026-10-23")
    assert histories["succeeded"] == 2
    assert all(len(item["snapshots"]) == 2 for item in histories["devices"])
    comparison = reader.compare_device_snapshots(serials + ["UNSEEN"], iso(earlier), iso(NOW))
    assert comparison["succeeded"] == 2 and comparison["failed"] == 1
    assert comparison["devices"][2]["error_code"] == "observation_not_retained"


@pytest.mark.parametrize("selection", ["000001", ["000001", "000002", "DENIED"]])
def test_actual_mcp_calls_accept_runtime_union_parameter(runtime, selection):
    settings, client, _, requests = runtime
    server = create_server(settings, client=client, clock=lambda: NOW)

    async def check():
        async with create_connected_server_and_client_session(server._mcp_server) as session:
            result = await session.call_tool("get_device_details", {"serial_numbers": selection})
            assert not result.isError
            batch = json.loads(result.content[0].text)
            assert batch["requested"] == (1 if isinstance(selection, str) else 3)
            assert batch["succeeded"] == (1 if isinstance(selection, str) else 2)
            assert batch["failed"] == (0 if isinstance(selection, str) else 1)

    asyncio.run(check())
    assert "000001" in requests


@pytest.mark.parametrize("serials", [["000001"], ["000001", "000002"]])
def test_cli_worker_accepts_runtime_serials_without_config(tmp_path, monkeypatch, serials):
    from device_sre import cli
    captured = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SRE_CONFIG", str(tmp_path / "no-config.json"))
    monkeypatch.setenv("SRE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["device-sre", "once", "--serial-numbers", *serials])

    async def fake_worker(settings, once=False):
        captured.append(settings.devices)
        assert once
        return {"failed": 0}

    monkeypatch.setattr(cli, "run_worker", fake_worker)
    cli.main()
    assert captured == [tuple(serials)]


def test_normalizer_preserves_order_and_leading_zeros():
    assert normalize_serial_numbers(["0001", "002", "0001"]) == ("0001", "002")


def test_overlapping_requests_return_their_own_readings(tmp_path):
    async def check():
        first_started, release_first = asyncio.Event(), asyncio.Event()

        class OverlappingClient:
            count = 0

            async def fetch(self, serial, clock):
                self.count += 1
                first = self.count == 1
                observed = NOW if first else NOW + timedelta(seconds=1)
                if first:
                    first_started.set()
                    await release_first.wait()
                return normalize(payload(serial, observed, ram=20 if first else 70), serial, observed)

        reader = Tools(Settings(data_dir=tmp_path), Store(tmp_path),
                       clock=lambda: NOW + timedelta(seconds=1), client=OverlappingClient())
        first = asyncio.create_task(reader.get_device_details("000001"))
        await first_started.wait()
        second = await reader.get_device_details("000001")
        release_first.set()
        earlier = await first
        assert earlier["devices"][0]["snapshot"]["metrics"]["ram_used_percent"] == 20
        assert second["devices"][0]["snapshot"]["metrics"]["ram_used_percent"] == 70
        assert reader.store.latest("000001")["metrics"]["ram_used_percent"] == 70

    asyncio.run(check())


def test_batch_respects_concurrency_setting(runtime):
    settings, _, reader, _ = runtime

    class BoundedClient:
        active = maximum = 0

        async def fetch(self, serial, clock):
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            await asyncio.sleep(0)
            self.active -= 1
            return normalize(payload(serial, NOW), serial, NOW)

    client = BoundedClient()
    reader.settings = replace(settings, concurrency=2)
    reader.client = client
    result = asyncio.run(reader.get_device_details([f"00000{index}" for index in range(5)]))
    assert result["succeeded"] == 5 and client.maximum == 2


@pytest.mark.parametrize("selection", ["000001", ["000001", "000002"]])
def test_every_mcp_refresh_requests_mst_again_even_with_an_unchanged_cached_sample(runtime, selection):
    settings, client, _, requests = runtime
    current_time = NOW
    server = create_server(settings, client=client, clock=lambda: current_time)
    serials = [selection] if isinstance(selection, str) else selection

    async def check():
        nonlocal current_time
        async with create_connected_server_and_client_session(server._mcp_server) as session:
            for index in range(2):
                current_time = NOW + timedelta(minutes=index)
                reply = await session.call_tool("get_device_details", {"serial_numbers": selection})
                assert not reply.isError
                result = json.loads(reply.content[0].text)
                assert result["fetched_from_mst"] is True
                assert result["fetched_from_mst_count"] == len(serials)
                assert result["data_source"] == "mst"
                assert result["succeeded"] == len(serials)
                for device in result["devices"]:
                    assert device["status"] == "fetched_observation"
                    assert device["fetched_from_mst"] is True
                    assert device["request_source"] == device["data_source"] == "mst"
                    if index:
                        # The API was called again, but its underlying sample did not change.
                        assert device["evaluation"]["baseline_status"] == "no_new_telemetry"
                        assert device["evaluation"]["deltas"] == {}
            assert all(requests.count(serial) == 2 for serial in serials)
            request_count = len(requests)
            cached = await session.call_tool("get_device_details", {"serial_numbers": selection, "refresh": False})
            result = json.loads(cached.content[0].text)
            assert len(requests) == request_count
            assert result["fetched_from_mst"] is False and result["fetched_from_mst_count"] == 0
            assert result["data_source"] == "cache"
            assert all(device["fetched_from_mst"] is False for device in result["devices"])

    asyncio.run(check())


def test_failed_refresh_never_labels_previous_cached_success_as_live(runtime):
    settings, _, reader, _ = runtime
    first = asyncio.run(reader.get_device_details("000001"))
    assert first["fetched_from_mst"] is True
    reader.clock = lambda: NOW + timedelta(minutes=15)
    reader.client = MSTClient(settings, transport=httpx.MockTransport(lambda request: httpx.Response(403)))
    failed = asyncio.run(reader.get_device_details("000001"))
    assert failed["fetched_from_mst"] is False and failed["fetched_from_mst_count"] == 0
    device = failed["devices"][0]
    assert device["snapshot"] == first["devices"][0]["snapshot"]
    assert device["status"] == "fetch_failed"
    assert device["error_code"] == "mst_http_403"
    assert device["fetched_from_mst"] is False
    assert device["data_source"] == "cache" and device["request_source"] == "mst"
