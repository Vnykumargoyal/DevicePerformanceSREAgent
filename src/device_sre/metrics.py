"""Parse the actual MST TerminalAnalyticsDTO; retain only performance fields."""
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo
import math

UTC = timezone.utc
NORMALIZATION_VERSION = 2


class PayloadError(ValueError):
    pass


def as_time(value) -> datetime | None:
    if isinstance(value, dict):
        value = value.get("$date", value.get("$numberLong"))
        if isinstance(value, dict):
            value = value.get("$numberLong")
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
            number = float(value)
            return datetime.fromtimestamp(number / 1000 if abs(number) > 1e11 else number, UTC)
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.astimezone(UTC) if parsed.tzinfo else None
    except (ValueError, OverflowError, OSError):
        pass
    return None


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def boundary(value: str, timezone_name: str = "Asia/Kolkata") -> datetime:
    """Date-only values mean midnight in the requested zone; timestamps need offsets."""
    if len(value) == 10:
        return datetime.combine(date.fromisoformat(value), time.min, ZoneInfo(timezone_name)).astimezone(UTC)
    parsed = as_time(value)
    if parsed is None:
        raise ValueError("Use YYYY-MM-DD or an ISO 8601 timestamp with a timezone offset")
    return parsed


def number(value) -> float | None:
    if isinstance(value, dict):
        value = value.get("$numberLong", value.get("$numberDouble", value.get("$numberInt")))
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def mapping(value) -> dict:
    return value if isinstance(value, dict) else {}


def text(value, limit=128):
    return value if isinstance(value, str) and 0 < len(value) <= limit else None


def byte_count(value) -> int | None:
    if isinstance(value, dict):
        value = value.get("$numberLong", value.get("$numberInt"))
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value) if len(value) <= 20 else None
    parsed = number(value)
    return int(parsed) if parsed is not None and parsed >= 0 and parsed.is_integer() else None


def software_context(data, details, serial, quality):
    info = mapping(details.get("device_info"))
    device_info = {key: value for key in ("os_version", "firmware_version", "build_number")
                   if (value := text(info.get(key))) is not None}
    api_level = number(info.get("android_api_level"))
    if api_level is not None and api_level.is_integer() and 0 <= api_level <= 1000:
        device_info["android_api_level"] = int(api_level)
    app = mapping(data.get("app_data"))
    if app.get("device_id") is not None and app["device_id"] != serial:
        raise PayloadError("app_device_identity_mismatch")
    versions, complete = {}, False
    source = updated = None
    inventory = app.get("app_data")
    if app.get("device_id") == serial and isinstance(inventory, list):
        source, updated = as_time(app.get("timestamp")), as_time(app.get("last_updated"))
        complete = len(inventory) <= 200
        duplicates = set()
        for item in inventory[:200]:
            item = mapping(item)
            package, version = text(item.get("package_name")), text(item.get("app_version"))
            if package is None or version is None:
                complete = False
            elif package in versions or package in duplicates:
                complete = False
                duplicates.add(package)
                versions.pop(package, None)
            else:
                versions[package] = version
        if not complete:
            quality.append("incomplete_app_inventory")
        if source is None:
            quality.append("missing_app_source_timestamp")
    elif isinstance(inventory, list) and inventory:
        quality.append("missing_app_device_identity")
    return {"device_info": device_info, "app_versions": versions,
            "app_source_timestamp": iso(source) if source else None,
            "app_last_updated": iso(updated) if updated else None,
            "app_inventory_complete": complete}


def normalize(payload: dict, serial: str, observed_at: datetime, signal_units=None) -> dict:
    # MST's TerminalAnalyticsDTO {data: {device_details: ..., app_data: ...}}.
    data = mapping(mapping(payload).get("data"))
    details = mapping(data.get("device_details"))
    metrics = mapping(details.get("device_metrics"))
    if not details or not metrics:
        # MST and analytics intentionally return HTTP 200 with null/empty data on errors.
        raise PayloadError("empty_device_details")
    if details.get("device_id") != serial:
        raise PayloadError("device_identity_mismatch")
    source = as_time(details.get("timestamp"))
    updated = as_time(details.get("last_updated"))
    source_field = "timestamp" if source else "last_updated"
    source = source or updated
    if source is None:
        raise PayloadError("missing_source_timestamp")
    values, quality = {}, []
    for resource in ("ram", "storage"):
        block = mapping(metrics.get(resource))
        used, total = byte_count(block.get("usedbytes")), byte_count(block.get("totalbytes"))
        if used is not None and total is not None and total > 0 and 0 <= used <= total:
            values[f"{resource}_used_bytes"] = used
            values[f"{resource}_total_bytes"] = total
            values[f"{resource}_used_percent"] = round(100 * used / total, 3)
            values[f"{resource}_available_bytes"] = total - used
            if "availablebytes" in block:
                reported = byte_count(block["availablebytes"])
                if reported is not None:
                    values[f"{resource}_reported_available_bytes"] = reported
                if reported != total - used:
                    quality.append(f"{resource}_available_bytes_mismatch")
        else:
            quality.append(f"missing_or_invalid_{resource}")
    cpu = mapping(metrics.get("cpu"))
    if "usedpercentage" in cpu:
        used = number(cpu["usedpercentage"])
        if used is not None and 0 <= used <= 100:
            values["cpu_used_percent"] = round(used, 3)
        else:
            quality.append("invalid_cpu_used_percent")
    for block, field, name in ((cpu, "totalusage", "cpu_total_usage_raw"),
                               (mapping(metrics.get("temperature")), "cputemp", "cpu_temperature_raw"),
                               (mapping(metrics.get("temperature")), "gputemp", "gpu_temperature_raw"),
                               (mapping(metrics.get("battery")), "temperature", "battery_temperature_raw"),
                               (mapping(metrics.get("battery")), "level", "battery_level_raw")):
        value = number(block.get(field))
        if value is not None:
            values[name] = value
    battery = mapping(metrics.get("battery"))
    if (health := text(battery.get("health"), 64)) is not None:
        values["battery_health_reported"] = health
    bool_fields = [(battery, "ischarging", "battery_is_charging")]
    mode = number(metrics.get("connectionmode"))
    if mode in (0, 1):
        values["connection_mode"] = int(mode)
    # These blocks are top-level fields on the Mongo device document.
    for interface, block in (("wifi", "wifi_metrics"), ("cellular", "cellular_metrics")):
        network = mapping(details.get(block))
        signal = number(network.get("signalstrength"))
        if signal is not None:
            values[f"{interface}_signal_raw"] = signal
            if (signal_units or {}).get(interface) == "dBm":
                if -140 <= signal < 0:
                    values[f"{interface}_signal_dbm"] = signal
                else:
                    quality.append(f"invalid_{interface}_signal_dbm")
        bool_fields.extend((network, source_name, target) for source_name, target in
                           (("enabled", f"{interface}_enabled"), ("connected", f"{interface}_connected"),
                            ("dataenabled", f"{interface}_data_enabled")))
        for source_name in ("networktype", "connectiontype"):
            if (value := text(network.get(source_name), 64)) is not None:
                values[f"{interface}_{source_name}"] = value
        stats = mapping(network.get("networkstats"))
        for field in ("downloadspeed", "uploadspeed", "latency"):
            if (value := number(stats.get(field))) is not None:
                values[f"{interface}_{field}_raw"] = value
    for block, field, name in bool_fields:
        if type(block.get(field)) is bool:
            values[name] = block[field]
        elif field in block:
            quality.append(f"invalid_{name}")
    mqtt = metrics.get("mqttstatus")
    if isinstance(mqtt, list):
        states = [mapping(item).get("connected") for item in mqtt]
        values["mqtt_connection_count"] = len(states)
        values["mqtt_connected_count"] = sum(state is True for state in states)
        values["mqtt_disconnected_count"] = sum(state is False for state in states)
        values["mqtt_unknown_count"] = sum(type(state) is not bool for state in states)
    for value, name in ((battery.get("batteryerror"), "battery_error_raw"),
                        (metrics.get("printererror"), "printer_error_raw"),
                        (metrics.get("carddiperror"), "card_dip_error_raw"),
                        (metrics.get("cardswipeerror"), "card_swipe_error_raw"),
                        (metrics.get("cardtaperror"), "card_tap_error_raw"),
                        (metrics.get("padnointernetcounter"), "no_internet_counter_raw")):
        parsed = number(value)
        if parsed is not None and parsed.is_integer() and -1 <= parsed <= 2**53:
            values[name] = int(parsed)
    online = details.get("is_device_online")
    if type(online) is not bool:
        online = None
        quality.append("missing_online_status")
    if (source - observed_at).total_seconds() > 120:
        quality.append("source_clock_in_future")
    software = software_context(data, details, serial, quality)
    return {
        "device_id": serial, "observed_at": iso(observed_at),
        "source_timestamp": iso(source), "source_timestamp_field": source_field,
        "last_updated": iso(updated) if updated else None,
        "is_device_online": online, "metrics": values, "data_quality": quality,
        "signal_units": signal_units or {"wifi": None, "cellular": None},
        "normalization_version": NORMALIZATION_VERSION,
        "device_context": {key: value for key in ("model", "manufacturer")
                           if (value := text(details.get(key), 64)) is not None},
        "software": software,
    }

