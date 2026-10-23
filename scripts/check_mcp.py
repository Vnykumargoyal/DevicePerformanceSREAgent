"""Local MCP test client. Reads credentials from .env; never prints them."""
import argparse
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys

from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

from device_sre.settings import read_secret, normalize_serial_numbers

ROOT = Path(__file__).resolve().parents[1]


@asynccontextmanager
async def connection(transport):
    if transport == "stdio":
        inherited = {key: os.environ[key] for key in
                     ("SRE_CONFIG", "SRE_DATA_DIR", "SRE_ALLOWED_HOSTS", "MST_BASE_URL",
                      "MST_TOKEN_FILE", "MST_BEARER_TOKEN")
                     if key in os.environ}
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "device_sre", "serve", "--transport", "stdio"],
            cwd=str(ROOT), env=inherited,
        )
        async with stdio_client(parameters) as (read, write):
            yield read, write
    else:
        # This test helper sends the connector credential only to loopback.
        port = int(os.getenv("SRE_PORT", "8765"))
        if not 1 <= port <= 65535:
            raise ValueError("SRE_PORT must be between 1 and 65535")
        token = read_secret("SRE_MCP_TOKEN_FILE", "SRE_MCP_BEARER_TOKEN")
        async with streamablehttp_client(
            f"http://127.0.0.1:{port}/mcp", headers={"Authorization": "Bearer " + token},
        ) as (read, write, _):
            yield read, write


async def check(args):
    serials = args.serial_numbers or args.device
    calls = [] if args.details_only else [("get_monitor_health", {})]
    if serials:
        calls.append(("get_device_details", {"serial_numbers": serials, "refresh": not args.cached}))
    if not args.details_only:
        calls.extend([("list_monitored_devices", {}), ("get_fleet_summary", {"serial_numbers": serials} if serials else {})])
    if args.start:
        calls.append(("get_device_history", {"serial_numbers": serials, "start": args.start,
                                             "end": args.end, "timezone_name": "Asia/Kolkata"}))
    if args.previous:
        calls.append(("compare_device_snapshots", {"serial_numbers": serials,
                     "previous_observed_at": args.previous, "current_observed_at": args.current}))
    failed = False
    async with connection(args.transport) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            if not args.details_only:
                tools = await session.list_tools()
                print("Connected. Available MCP tools:")
                for tool in tools.tools:
                    print("  " + tool.name)
            for name, arguments in calls:
                result = await session.call_tool(name, arguments)
                outputs = []
                for content in result.content:
                    if content.type == "text":
                        try:
                            outputs.append(json.loads(content.text))
                        except ValueError:
                            outputs.append(content.text)
                device_failures = any(isinstance(value, dict) and value.get("failed", 0) for value in outputs)
                label = " [ERROR]" if result.isError else " [DEVICE FAILURES]" if device_failures else " [OK]"
                if not args.details_only:
                    print("\n" + name + label)
                for value in outputs:
                    print(json.dumps(value, indent=2) if isinstance(value, (dict, list)) else value)
                failed = failed or bool(result.isError) or device_failures
    return 2 if failed else 0


def main():
    os.chdir(ROOT)
    load_dotenv(ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(description="Test this local MCP server without exposing tokens")
    parser.add_argument("--transport", choices=("stdio", "http"), default="http")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--serial-numbers", nargs="+", help="One or more serial strings passed at runtime")
    selection.add_argument("--device", action="append", help="Serial string; can be repeated (legacy alias)")
    parser.add_argument("--cached", action="store_true", help="Read existing snapshots without fetching MST")
    parser.add_argument("--details-only", action="store_true", help="Print only the get_device_details JSON response")
    parser.add_argument("--start", help="History start date or offset timestamp, inclusive")
    parser.add_argument("--end", help="History end date or offset timestamp, exclusive")
    parser.add_argument("--previous", help="Exact previous observed_at from history")
    parser.add_argument("--current", help="Exact current observed_at from history")
    args = parser.parse_args()
    if args.details_only and (not (args.serial_numbers or args.device) or args.start or args.end or args.previous or args.current):
        parser.error("--details-only requires --serial-numbers and cannot be combined with history/comparison options")
    if bool(args.start) != bool(args.end) or bool(args.previous) != bool(args.current):
        parser.error("Supply both --start/--end or both --previous/--current")
    if (args.start or args.previous) and not (args.serial_numbers or args.device):
        parser.error("--serial-numbers is required for history or comparison")
    try:
        if args.serial_numbers or args.device:
            normalize_serial_numbers(args.serial_numbers or args.device, max_devices=100)
        code = asyncio.run(check(args))
    except ValueError as exc:
        parser.exit(1, f"Configuration error: {exc}\n")
    except Exception as exc:
        # SDK connection errors may wrap sensitive request details. Print only the type.
        parser.exit(1, f"MCP connection failed ({type(exc).__name__}). Check the local server, port and MCP token file.\n")
    raise SystemExit(code)


if __name__ == "__main__":
    main()
