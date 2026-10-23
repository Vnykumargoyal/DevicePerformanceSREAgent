import asyncio
import json
import sys
import subprocess
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from device_sre.server import TOOL_NAMES
from device_sre.metrics import as_time, normalize
from device_sre.store import Store


def test_real_stdio_subprocess_handshake_and_tools(tmp_path):
    params = StdioServerParameters(command=sys.executable,
                                  args=["-m", "device_sre", "serve", "--transport", "stdio"],
                                  cwd=str(tmp_path),
                                  env={"SRE_CONFIG": str(tmp_path / "monitor.json"),
                                       "SRE_DATA_DIR": str(tmp_path / "cache")})

    async def check():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listing = await session.list_tools()
                assert {t.name for t in listing.tools} == set(TOOL_NAMES)
                result = await session.call_tool("get_fleet_summary", {})
                assert not result.isError
                assert json.loads(result.content[0].text)["coverage"]["total"] == 0

    asyncio.run(check())


def test_check_client_labels_device_failure_instead_of_ok(tmp_path, monkeypatch):
    root = Path(__file__).parents[1]
    monkeypatch.setenv("SRE_CONFIG", str(tmp_path / "no-config.json"))
    monkeypatch.setenv("SRE_DATA_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("MST_TOKEN_FILE", str(tmp_path / "missing-token"))
    completed = subprocess.run([
        sys.executable, str(root / "scripts" / "check_mcp.py"),
        "--transport", "stdio", "--serial-numbers", "NEW-DEVICE",
    ], cwd=root, capture_output=True, text=True, timeout=30)
    assert completed.returncode == 2, completed.stderr
    assert "get_device_details [DEVICE FAILURES]" in completed.stdout
    assert "get_device_details [OK]" not in completed.stdout
    assert "mst_credential_unavailable" in completed.stdout
    assert "No usable MST credential" in completed.stdout


def test_details_only_returns_device_json_without_other_checks(tmp_path, monkeypatch):
    root = Path(__file__).parents[1]
    body = json.loads((root / "tests" / "fixtures" / "device_response.json").read_text())
    serial = body["data"]["device_details"]["device_id"]
    now = as_time(body["data"]["device_details"]["timestamp"])
    cache = tmp_path / "cache"
    Store(cache).record("fixture-run", serial, now, snapshot=normalize(body, serial, now))
    monkeypatch.setenv("SRE_CONFIG", str(tmp_path / "no-config.json"))
    monkeypatch.setenv("SRE_DATA_DIR", str(cache))
    monkeypatch.setenv("MST_TOKEN_FILE", str(tmp_path / "missing-token"))
    completed = subprocess.run([
        sys.executable, str(root / "scripts" / "check_mcp.py"),
        "--transport", "stdio", "--serial-numbers", serial, "--details-only", "--cached",
    ], cwd=root, capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["succeeded"] == 1 and result["failed"] == 0
    assert result["fetched_from_mst"] is False and result["data_source"] == "cache"
    assert result["devices"][0]["snapshot"]["metrics"]["cpu_used_percent"] == 18.818
    assert "Connected. Available MCP tools:" not in completed.stdout


def test_details_only_live_failure_is_json_with_actionable_error(tmp_path, monkeypatch):
    root = Path(__file__).parents[1]
    monkeypatch.setenv("SRE_CONFIG", str(tmp_path / "no-config.json"))
    monkeypatch.setenv("SRE_DATA_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("MST_TOKEN_FILE", str(tmp_path / "missing-token"))
    completed = subprocess.run([
        sys.executable, str(root / "scripts" / "check_mcp.py"),
        "--transport", "stdio", "--serial-numbers", "NEW-DEVICE", "--details-only",
    ], cwd=root, capture_output=True, text=True, timeout=30)
    assert completed.returncode == 2, completed.stderr
    result = json.loads(completed.stdout)
    assert result["failed"] == 1
    assert result["fetched_from_mst"] is False and result["fetched_from_mst_count"] == 0
    assert result["devices"][0]["error_code"] == "mst_credential_unavailable"
    assert "configure-token" in result["devices"][0]["error_details"]["next_steps"][0]

