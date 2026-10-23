"""Fetch live device details through MCP by default; use --offline to replay saved JSON."""
import argparse
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import logging
import os
import re
import shlex
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx
from mcp.shared.memory import create_connected_server_and_client_session

from device_sre.metrics import as_time, iso, mapping
from device_sre.mst import MSTClient, FetchError
from device_sre.server import create_server
from device_sre.settings import Settings, normalize_serial_numbers, validate_device_id

ROOT = Path(__file__).resolve().parents[1]


class ReplaySelectionError(ValueError):
    pass


async def replay_responses(payloads, serial_numbers, assessed_at, settings):
    serials = normalize_serial_numbers(serial_numbers, max_devices=100)
    by_serial = {}
    for payload in payloads:
        details = mapping(mapping(mapping(payload).get("data")).get("device_details"))
        serial = validate_device_id(details.get("device_id"))
        if serial in by_serial:
            raise ValueError("Supply only one response file per device in a replay")
        by_serial[serial] = payload
    if not any(serial in by_serial for serial in serials):
        raise ReplaySelectionError(
            "Offline replay stopped: no supplied response matches the requested serial(s): " + ", ".join(serials)
            + "\nAvailable response serial(s): " + ", ".join(by_serial)
            + "\nNo MST API was called. For live data, save the approved MST token using ./run-local.sh configure-token"
            + "\nThen run: .venv/bin/python scripts/check_mcp.py --transport stdio --serial-numbers "
            + shlex.join(serials) + " --details-only"
        )
    requests = []

    def respond(request):
        # MockTransport handles every request: this script cannot fall back to UAT.
        serial = request.headers["hardware-serial-number"]
        requests.append(serial)
        if serial not in by_serial:
            raise FetchError("replay_sample_missing")
        return httpx.Response(200, json=by_serial[serial])

    async def call(session, name, arguments):
        result = await session.call_tool(name, arguments)
        if result.isError:
            raise ValueError(f"MCP tool rejected the replay: {name}")
        return json.loads(result.content[0].text)

    with TemporaryDirectory(prefix="device-sre-mcp-replay-") as directory:
        replay_settings = replace(settings, devices=(), data_dir=Path(directory))
        client = MSTClient(replay_settings, transport=httpx.MockTransport(respond), data_source="replay")
        server = create_server(replay_settings, client=client, clock=lambda: assessed_at)
        # Avoid HTTP-looking INFO logs for requests that are entirely mocked.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        # Only this offline utility supplies a dummy credential. Production continues
        # to use read_secret; no existing credential file or environment is read/changed.
        with patch("device_sre.mst.read_secret", return_value="offline-replay-dummy-token"):
            async with create_connected_server_and_client_session(server._mcp_server) as session:
                listing = await session.list_tools()
                details = await call(session, "get_device_details", {"serial_numbers": serial_numbers})
                fetch_count = len(requests)
                cached = await call(session, "get_device_details", {"serial_numbers": serial_numbers, "refresh": False})
                history = await call(session, "get_device_history", {
                    "serial_numbers": serial_numbers,
                    "start": iso(assessed_at - timedelta(microseconds=1)),
                    "end": iso(assessed_at + timedelta(microseconds=1)),
                })
                fleet = await call(session, "get_fleet_summary", {"serial_numbers": serial_numbers})
        report = {
            "evidence_source": "user_supplied_responses_replayed_through_mcp",
            "transport": "MCP SDK in-memory client/server session",
            "fetched_from_mst": False,
            "operational_cache_modified": False,
            "assessed_at": iso(assessed_at),
            "mcp_tool": "get_device_details",
            "tool_arguments": {"serial_numbers": serial_numbers},
            "mcp_tool_result": details,
            "verification": {
                "discovered_tools": [tool.name for tool in listing.tools],
                "mock_mst_requests": len(requests),
                "one_request_per_unique_serial": len(requests) == len(serials),
                "cache_reads_did_not_fetch": len(requests) == fetch_count,
                "successful_observations": details["succeeded"],
                "cached_snapshots_match": details["succeeded"] > 0 and [item["snapshot"] for item in cached["devices"]]
                                          == [item["snapshot"] for item in details["devices"]],
                "history_observations_per_device": {item["device_id"]: len(item["snapshots"])
                                                    for item in history["devices"]},
                "fleet_coverage": fleet["coverage"],
            },
            "comparison_note": ("Successful supplied samples create a baseline per device. A second distinct source reading is required for a trend."
                                if details["succeeded"] else "No supplied response passed validation; no baseline was created."),
        }
    report["temporary_replay_cache_removed"] = True
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true",
                        help="Read supplied response files only; no real credential or network access")
    parser.add_argument("--file", type=Path, action="append",
                        help="Saved response JSON for --offline; ignored in default live mode")
    parser.add_argument("--serial-numbers", nargs="+",
                        help="One serial or several serials; prompts for input when omitted in a terminal")
    parser.add_argument("--assessed-at", help="Offline-only ISO timestamp with offset for reproducible assessment")
    args = parser.parse_args()
    if args.serial_numbers is None:
        if not sys.stdin.isatty():
            parser.error("Pass --serial-numbers when running without an interactive terminal")
        print("Enter device serial number(s), separated by commas or spaces: ", end="", file=sys.stderr, flush=True)
        try:
            entered = input()
        except (EOFError, KeyboardInterrupt):
            parser.exit(1, "\nNo serial numbers entered; no API request was sent.\n")
        args.serial_numbers = [value for value in re.split(r"[,\s]+", entered.strip()) if value]
    try:
        serials = normalize_serial_numbers(args.serial_numbers, max_devices=100)
    except ValueError as exc:
        parser.error(str(exc))
    if not args.offline:
        if args.assessed_at:
            parser.error("--assessed-at requires --offline; live observation times come from the API request")
        if args.file:
            print("Live mode: --file is ignored. Fetching the requested serials through MCP and MST. "
                  "Add --offline only to replay saved responses.", file=sys.stderr)
        # Keep the user's existing command working. Replace this process with the
        # same stdio MCP client used by check_mcp.py; it loads the configured token
        # and collects through MST on every call. No supplied file is opened here.
        try:
            return os.execv(sys.executable, [sys.executable, str(ROOT / "scripts" / "check_mcp.py"),
                                            "--transport", "stdio", "--serial-numbers", *serials, "--details-only"])
        except OSError:
            parser.exit(1, "Cannot start the local MCP client. Check the project's Python environment.\n")
    if not args.file:
        parser.error("--offline requires at least one --file containing the requested device response")
    now = as_time(args.assessed_at) if args.assessed_at else datetime.now(timezone.utc)
    if now is None:
        parser.error("--assessed-at must be an ISO timestamp with a timezone offset")
    try:
        payloads = [json.loads(path.read_text()) for path in args.file]
        selection = args.serial_numbers[0] if len(args.serial_numbers) == 1 else args.serial_numbers
        report = asyncio.run(replay_responses(payloads, selection, now, Settings.load()))
    except ReplaySelectionError as exc:
        parser.exit(2, str(exc) + "\n")
    except OSError:
        parser.exit(1, "Cannot read a supplied response file or create the temporary cache.\n")
    except ValueError as exc:
        parser.exit(1, f"Replay/configuration error: {exc}\n")
    print(json.dumps(report, indent=2, allow_nan=False))
    return 2 if report["mcp_tool_result"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
