"""Inspect a user-supplied MST response offline, without enrolling a real device."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from device_sre.analysis import compare
from device_sre.metrics import as_time, iso, normalize
from device_sre.settings import Settings, validate_device_id


def inspect_response(payload, serial, assessed_at, settings):
    validate_device_id(serial)
    snapshot = normalize(payload, serial, assessed_at, settings.signal_units)
    return {
        "evidence_source": "user_supplied_response_file",
        "fetched_from_mst": False, "stored_in_monitor_cache": False,
        "assessed_at": iso(assessed_at), "distinct_samples": 1,
        "source_time_india": as_time(snapshot["source_timestamp"]).astimezone(ZoneInfo("Asia/Kolkata")).isoformat(),
        "snapshot": snapshot, "assessment": compare(None, snapshot, settings, assessed_at),
        "comparison_note": "One supplied snapshot only. Previous values and time-series changes are unavailable.",
    }


def main():
    parser = argparse.ArgumentParser(description="Validate and assess a supplied device response without calling MST")
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--serial-number", required=True)
    parser.add_argument("--assessed-at", help="Optional ISO timestamp with offset for reproducible assessment")
    args = parser.parse_args()
    now = as_time(args.assessed_at) if args.assessed_at else datetime.now(timezone.utc)
    if now is None:
        parser.error("--assessed-at must be an ISO timestamp with a timezone offset")
    try:
        payload = json.loads(args.file.read_text())
        result = inspect_response(payload, args.serial_number, now, Settings.load())
    except OSError:
        parser.exit(1, "Cannot read the supplied response file.\n")
    except ValueError as exc:
        parser.exit(1, f"Response/configuration error: {exc}\n")
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
