"""Verify HTTP MCP authentication/discovery, without retrieving device data."""
import argparse
import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

EXPECTED_TOOLS = {
    "get_monitor_health", "list_monitored_devices", "get_device_details",
    "get_device_history", "compare_device_snapshots", "get_fleet_summary",
}


async def check(url, token_path):
    parsed = urlsplit(url)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path != "/mcp"):
        raise ValueError("Use an HTTPS /mcp URL without credentials, query or fragment")
    if parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("Plain HTTP is allowed only on loopback; use HTTPS for a remote host")
    token = Path(token_path).read_text().strip()
    if len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
        raise ValueError("The MCP token file must contain the dedicated raw connector token")
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        denied = await client.post(url, json={})
        if denied.status_code != 401:
            raise ValueError("Unauthenticated MCP requests must return HTTP 401")
        async with streamablehttp_client(
                url, headers={"Authorization": "Bearer " + token},
                httpx_client_factory=lambda **kwargs: httpx.AsyncClient(
                    follow_redirects=False, **kwargs)) as (reader, writer, _):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                listed = await session.list_tools()
                names = {tool.name for tool in listed.tools}
                if names != EXPECTED_TOOLS:
                    raise ValueError("The server did not discover exactly the expected six tools")
                health = await session.call_tool("get_monitor_health", {})
                if health.isError:
                    raise ValueError("Authenticated monitor health tool failed")
    print(json.dumps({"authentication": "passed", "discovered_tools": sorted(names),
                      "health_tool": "passed", "mst_called": False}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8765/mcp")
    parser.add_argument("--token-file", required=True)
    args = parser.parse_args()
    try:
        asyncio.run(check(args.url, args.token_file))
    except (ValueError, OSError) as exc:
        # Do not include credential-bearing HTTP requests or upstream responses.
        print(f"MCP smoke check failed ({type(exc).__name__}). Check URL, token and server logs.", file=sys.stderr)
        return 1
    except Exception:
        print("MCP smoke check failed. Check connectivity, HTTPS, authentication and server logs.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
