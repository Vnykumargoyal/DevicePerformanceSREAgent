import asyncio
import json
import random
import uuid
import httpx

from .metrics import PayloadError, normalize
from .settings import Settings, read_secret


class FetchError(RuntimeError):
    """Safe code only. Upstream bodies and credentials never enter error output."""


class MSTClient:
    def __init__(self, settings: Settings, transport=None, sleep=asyncio.sleep, *, data_source="mst"):
        if data_source not in ("mst", "replay"):
            raise ValueError("Unsupported MST adapter data source")
        self.settings = settings
        self.transport = transport
        self.sleep = sleep
        self.data_source = data_source

    async def fetch(self, serial: str, observed_clock):
        self.settings.require_device(serial)
        correlation_id = str(uuid.uuid4())
        async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds,
                                     follow_redirects=False, transport=self.transport) as client:
            for attempt in range(3):
                try:
                    token = read_secret("MST_TOKEN_FILE", "MST_BEARER_TOKEN")
                except ValueError:
                    raise FetchError("mst_credential_unavailable") from None
                try:
                    async with client.stream(
                        "GET", self.settings.base_url + "/v1/mst-sync/analytics/device-details",
                        headers={"Authorization": "Bearer " + token,
                                 "hardware-serial-number": serial,
                                 "x-correlation-id": correlation_id, "Accept": "application/json"},
                    ) as response:
                        status = response.status_code
                        if status == 429 or 500 <= status < 600:
                            retry_after = response.headers.get("retry-after", "")
                            wait = min(float(retry_after), 30) if retry_after.isdigit() else 2 ** attempt + random.random()
                            code = f"mst_http_{status}"
                        elif status != 200:
                            raise FetchError(f"mst_http_{status}")
                        else:
                            body = bytearray()
                            async for chunk in response.aiter_bytes():
                                body.extend(chunk)
                                if len(body) > 2_000_000:
                                    raise FetchError("mst_response_too_large")
                            try:
                                payload = json.loads(body)
                            except (ValueError, UnicodeError, RecursionError):
                                raise FetchError("mst_invalid_json") from None
                            try:
                                return normalize(payload, serial, observed_clock(), self.settings.signal_units)
                            except PayloadError as exc:
                                raise FetchError(str(exc)) from None
                except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError):
                    code, wait = "mst_transport_error", 2 ** attempt + random.random()
                if attempt == 2:
                    raise FetchError(code)
                await self.sleep(wait)

