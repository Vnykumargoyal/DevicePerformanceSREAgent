import hmac
import os
from urllib.parse import urlsplit

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.responses import HTMLResponse, JSONResponse

from .settings import read_secret
from .monitor import utc_now
from .store import Store
from .tools import Tools
from .landing import HOME_HEADERS, HOME_HTML

TOOL_NAMES = (
    "get_monitor_health", "list_monitored_devices", "get_device_details",
    "get_device_history", "compare_device_snapshots", "get_fleet_summary",
)


def create_server(settings, client=None, clock=utc_now):
    allowed_hosts = [s.strip() for s in os.getenv("SRE_ALLOWED_HOSTS", "127.0.0.1:*,localhost:*").split(",") if s.strip()]
    if not allowed_hosts or "*" in allowed_hosts:
        raise ValueError("SRE_ALLOWED_HOSTS must list exact trusted hostnames (port wildcards are allowed)")
    # Browser POSTs carry Origin. Trust configured connector hosts only.
    browser_origins = []
    for host in allowed_hosts:
        browser_origins.append("https://" + host)
        if urlsplit("//" + host.removesuffix(":*")).hostname in ("localhost", "127.0.0.1", "::1"):
            browser_origins.append("http://" + host)
    mcp = FastMCP(
        "Device Performance SRE", json_response=True, stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=allowed_hosts, allowed_origins=browser_origins),
        instructions="Pass one serial string or an array to get_device_details; MST authorizes access. "
                     "That tool fetches and caches observations; the other tools read retained cache. "
                     "For current online data, pass refresh=true and live_only=true; this withholds offline, old and cached metrics. "
                     "Analytics GET cannot trigger device collection. "
                     "Treat stale, missing and failed polls as unknown; deletion audit is not time-series history.",
    )
    reader = Tools(settings, Store(settings.data_dir), clock=clock, client=client)
    annotation = ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                 idempotentHint=True, openWorldHint=False)
    for name in TOOL_NAMES:
        hints = (ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                 idempotentHint=False, openWorldHint=True)
                 if name == "get_device_details" else annotation)
        mcp.tool(name=name, annotations=hints)(getattr(reader, name))
    return mcp


class BearerGuard:
    """Separate inbound MCP credential; never forward it as an MST token."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if scope["path"] == "/" and scope["method"] in ("GET", "HEAD"):
            return await HTMLResponse(HOME_HTML, headers=HOME_HEADERS)(scope, receive, send)
        if scope["path"] == "/healthz":
            return await JSONResponse({"status": "alive"})(scope, receive, send)
        try:
            expected = connector_secret()
        except ValueError:
            return await JSONResponse({"error": "connector_credential_unavailable"}, status_code=503)(scope, receive, send)
        headers = dict(scope.get("headers", []))
        actual = headers.get(b"authorization", b"")
        if not hmac.compare_digest(actual, ("Bearer " + expected).encode()):
            return await JSONResponse({"error": "unauthorized"}, status_code=401,
                                      headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
        return await self.app(scope, receive, send)


def connector_secret():
    token = read_secret("SRE_MCP_TOKEN_FILE", "SRE_MCP_BEARER_TOKEN")
    if len(token) < 32:
        raise ValueError("MCP connector token must contain at least 32 characters")
    return token


def serve(settings, transport):
    mcp = create_server(settings)
    if transport == "stdio":
        mcp.run(transport="stdio")
        return
    import uvicorn
    connector_secret()  # Fail closed before listening.
    uvicorn.run(BearerGuard(mcp.streamable_http_app()), host=os.getenv("SRE_HOST", "127.0.0.1"),
                port=int(os.getenv("SRE_PORT", "8765")), access_log=False, log_level="warning")

