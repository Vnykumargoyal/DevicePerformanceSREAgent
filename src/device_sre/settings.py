from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
import json
import math
import os
import re

DEFAULT_THRESHOLDS = {
    "ram_used_percent": 85.0,
    "storage_used_percent": 85.0,
    "cpu_used_percent": 85.0,
    "wifi_signal_dbm": -75.0,
    "cellular_signal_dbm": -105.0,
    "increase_percentage_points": 10.0,
}


def validate_device_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
        raise ValueError("Device serial must contain 1–128 letters, digits, _, ., : or -")
    return value


def normalize_serial_numbers(value, max_devices=1000) -> tuple[str, ...]:
    """Accept one serial or a batch, preserve leading zeros and remove duplicates."""
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, (list, tuple)) or not values or len(values) > 1000:
        raise ValueError("Pass a serial string or a nonempty list of up to 1000 serial strings")
    for serial in values:
        validate_device_id(serial)
    serials = tuple(dict.fromkeys(values))
    if len(serials) > max_devices:
        raise ValueError(f"Pass at most {max_devices} distinct serials per request")
    return serials


def read_secret(file_var: str, value_var: str) -> str:
    """Read current credentials without putting file contents into errors/logs."""
    path = os.getenv(file_var)
    try:
        secret = Path(path).expanduser().read_text().strip() if path else os.getenv(value_var, "").strip()
    except OSError:
        raise ValueError(f"Cannot read secret file configured by {file_var}") from None
    if not secret or not secret.isascii() or any(c.isspace() for c in secret):
        raise ValueError(f"Configure {file_var} or {value_var} with a nonempty token (without Bearer prefix)")
    return secret


@dataclass(frozen=True)
class Settings:
    devices: tuple[str, ...] = ()
    base_url: str = "https://api.tms.uat.pinelabs.com"
    data_dir: Path = Path("data")
    poll_seconds: int = 900
    retention_days: int = 7
    stale_seconds: int = 1800
    concurrency: int = 4
    request_timeout_seconds: int = 20
    thresholds: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_THRESHOLDS))
    signal_units: dict = field(default_factory=lambda: {"wifi": None, "cellular": None})

    def __post_init__(self):
        url = urlsplit(self.base_url)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.query or url.fragment or url.path not in ("", "/")):
            raise ValueError("MST_BASE_URL must be an HTTPS origin without credentials, path or query")
        for serial in self.devices:
            validate_device_id(serial)
        if len(self.devices) > 1000 or len(self.devices) != len(set(self.devices)):
            raise ValueError("Pass at most 1000 distinct device serials per worker")
        if (set(self.signal_units) != {"wifi", "cellular"}
                or any(v not in (None, "dBm") for v in self.signal_units.values())):
            raise ValueError("signal_units must declare wifi/cellular as null (unverified) or dBm (verified by owner)")
        for name, low, high in (("poll_seconds", 60, 86400), ("retention_days", 1, 365),
                                ("stale_seconds", 60, 86400), ("concurrency", 1, 32),
                                ("request_timeout_seconds", 1, 120)):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{name} must be an integer between {low} and {high}")
        if set(self.thresholds) != set(DEFAULT_THRESHOLDS):
            raise ValueError("thresholds must use the keys from monitor.example.json")
        for name, value in self.thresholds.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"Invalid threshold: {name}")
            low, high = (-140, -1) if name.endswith("dbm") else (0, 100)
            if not low <= value <= high:
                raise ValueError(f"{name} must be between {low} and {high}")

    @classmethod
    def load(cls):
        path = Path(os.getenv("SRE_CONFIG", "monitor.json"))
        raw = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(raw, dict) or not isinstance(raw.get("devices", []), list):
            raise ValueError("Monitor configuration must be an object; legacy devices, if present, must be an array")
        allowed = {"devices", "poll_seconds", "retention_days", "stale_seconds", "concurrency",
                   "request_timeout_seconds", "thresholds", "signal_units"}
        if set(raw) - allowed:
            raise ValueError("Unknown monitor configuration key")
        if "thresholds" in raw:
            if not isinstance(raw["thresholds"], dict) or set(raw["thresholds"]) - set(DEFAULT_THRESHOLDS):
                raise ValueError("Unknown threshold configuration key")
            raw["thresholds"] = {**DEFAULT_THRESHOLDS, **raw["thresholds"]}
        return cls(**{**raw, "devices": tuple(raw.get("devices", [])),
                      "base_url": os.getenv("MST_BASE_URL", cls.base_url).rstrip("/"),
                      "data_dir": Path(os.getenv("SRE_DATA_DIR", "data"))})

    def require_device(self, serial: str):
        # Device access is authorized by MST, not by a static local serial list.
        validate_device_id(serial)

