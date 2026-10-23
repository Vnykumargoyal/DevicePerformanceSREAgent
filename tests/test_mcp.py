import asyncio
import json

import httpx
from mcp import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session

from device_sre.server import BearerGuard, create_server, TOOL_NAMES
from device_sre.settings import Settings
from device_sre.mst import MSTClient


def test_mcp_tool_discovery_and_read_only_calls(tmp_path):
    server = create_server(Settings(data_dir=tmp_path))

    async def check():
        async with create_connected_server_and_client_session(server._mcp_server) as session:
            listing = await session.list_tools()
            assert {t.name for t in listing.tools} == set(TOOL_NAMES)
            assert all(not t.annotations.destructiveHint for t in listing.tools)
            assert all(t.annotations.readOnlyHint for t in listing.tools if t.name != "get_device_details")
            details = next(t for t in listing.tools if t.name == "get_device_details")
            assert not details.annotations.readOnlyHint  # Fetch persists a local snapshot.
            assert "serial_numbers" in details.inputSchema["required"]
            result = await session.call_tool("get_monitor_health", {})
            assert not result.isError
            value = json.loads(result.content[0].text)
            assert value["status"] == "awaiting_device_request"
            rejected = await session.call_tool("get_device_details", {"serial_numbers": "invalid\nserial"})
            assert rejected.isError

    asyncio.run(check())


def test_streamable_http_auth_and_jsonrpc(tmp_path, monkeypatch):
    monkeypatch.setenv("SRE_MCP_BEARER_TOKEN", "a" * 40)
    monkeypatch.delenv("SRE_MCP_TOKEN_FILE", raising=False)
    monkeypatch.delenv("SRE_ALLOWED_HOSTS", raising=False)
    server = create_server(Settings(data_dir=tmp_path))
    app = BearerGuard(server.streamable_http_app())

    async def check():
        async with server.session_manager.run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765") as client:
                assert (await client.get("/healthz")).json() == {"status": "alive"}
                assert (await client.post("/mcp", json={})).status_code == 401
                headers = {"Authorization": "Bearer " + "a" * 40,
                           "Accept": "application/json, text/event-stream"}
                response = await client.post("/mcp", headers=headers, json={
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                               "clientInfo": {"name": "test", "version": "1"}}})
                assert response.status_code == 200
                assert "serverInfo" in response.json()["result"]
                wrong_host = await client.post("/mcp", headers={**headers, "Host": "evil.invalid"}, json={})
                assert wrong_host.status_code == 421
                monkeypatch.setenv("SRE_MCP_BEARER_TOKEN", "b" * 40)
                assert (await client.post("/mcp", headers=headers, json={})).status_code == 401

    asyncio.run(check())


def test_missing_mst_credential_is_explained_without_sending_a_request(tmp_path, monkeypatch):
    monkeypatch.setenv("MST_TOKEN_FILE", str(tmp_path / "missing-token"))
    monkeypatch.delenv("MST_BEARER_TOKEN", raising=False)

    def unexpected_request(request):
        raise AssertionError("No upstream request may be sent without credentials")

    settings = Settings(data_dir=tmp_path)
    client = MSTClient(settings, transport=httpx.MockTransport(unexpected_request))
    server = create_server(settings, client=client)

    async def check():
        async with create_connected_server_and_client_session(server._mcp_server) as session:
            result = await session.call_tool("get_device_details", {"serial_numbers": "NEW-DEVICE"})
            assert not result.isError  # The batch reports the device failure explicitly.
            batch = json.loads(result.content[0].text)
            assert batch["failed"] == 1 and batch["succeeded"] == 0
            assert batch["fetched_from_mst"] is False and batch["fetched_from_mst_count"] == 0
            device = batch["devices"][0]
            assert device["snapshot"] is None
            assert device["fetched_from_mst"] is False and device["data_source"] == "unavailable"
            assert device["error_code"] == "mst_credential_unavailable"
            assert device["error_details"]["stage"] == "credential"
            assert "No usable MST credential" in device["error_details"]["message"]
            assert "MST_TOKEN_FILE" in device["error_details"]["next_steps"][0]

    asyncio.run(check())

