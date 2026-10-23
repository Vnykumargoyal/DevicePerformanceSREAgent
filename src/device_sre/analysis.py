from datetime import datetime
from .metrics import as_time, iso
from .settings import Settings


def software_comparison(previous, current, now, settings, device_comparable):
    result = {"device_info": {}, "apps": [], "apps_status": "unavailable"}
    new = current.get("software", {})
    old = previous.get("software", {}) if previous else {}
    if previous and device_comparable:
        for field, value in new.get("device_info", {}).items():
            prior = old.get("device_info", {}).get(field)
            if prior is not None and prior != value:
                result["device_info"][field] = {"previous": prior, "current": value}
    source = as_time(new.get("app_source_timestamp"))
    prior_source = as_time(old.get("app_source_timestamp"))
    if not source:
        return result
    age = (now - source).total_seconds()
    if age < -120 or age > settings.stale_seconds:
        result["apps_status"] = "stale_or_future_app_data"
        return result
    if not prior_source:
        result["apps_status"] = "baseline_created"
        return result
    if source < prior_source:
        result["apps_status"] = "out_of_order_app_data"
        return result
    if source == prior_source:
        result["apps_status"] = ("no_new_app_telemetry" if old.get("app_versions") == new.get("app_versions")
                                 else "app_timestamp_reused_with_changed_inventory")
        return result
    result["apps_status"] = "compared"
    old_apps, new_apps = old.get("app_versions", {}), new.get("app_versions", {})
    packages = set(old_apps) | set(new_apps) if old.get("app_inventory_complete") and new.get("app_inventory_complete") else set(old_apps) & set(new_apps)
    changes = [{"package_name": package, "previous": old_apps.get(package), "current": new_apps.get(package),
                "meaning": "reported version change; installation time and causality unverified"}
               for package in sorted(packages) if old_apps.get(package) != new_apps.get(package)]
    result["apps"] = changes[:50]
    result["apps_changes_truncated"] = len(changes) > 50
    return result


def compare(previous: dict | None, current: dict, settings: Settings, now: datetime) -> dict:
    source_age = (now - as_time(current["source_timestamp"])).total_seconds()
    cache_age = max(0, (now - as_time(current["observed_at"])).total_seconds())
    freshness = "future_clock" if source_age < -120 else "stale" if source_age > settings.stale_seconds else "fresh"
    findings = []

    def finding(code, severity, evidence, recommendation):
        findings.append({"code": code, "severity": severity, "evidence": evidence,
                         "recommendation": recommendation})

    if freshness != "fresh":
        finding("telemetry_" + freshness, "warning", {"source_age_seconds": round(source_age)},
                "Check collector delivery, connectivity and device clock before drawing performance conclusions.")
    if cache_age > settings.stale_seconds:
        finding("poller_data_stale", "warning", {"cache_age_seconds": round(cache_age)},
                "Check worker health and MST authorization/connectivity; this cached observation is old.")
    if current["is_device_online"] is False:
        finding("api_reports_offline", "warning", {"is_device_online": False},
                "Check terminal power, connectivity and the device-specific reporting interval.")
    if current["data_quality"]:
        finding("incomplete_metrics", "info", {"fields": current["data_quality"]},
                "Validate inconsistent/missing collector fields. Derive free bytes from total minus used; unknown measurements are not zero.")
    baseline_status = "baseline_created"
    interval = None
    deltas = {}
    if previous:
        interval = (as_time(current["observed_at"]) - as_time(previous["observed_at"])).total_seconds()
        if current.get("normalization_version", 1) != previous.get("normalization_version", 1):
            baseline_status = "normalization_version_changed"
            finding(baseline_status, "info", {},
                    "The adapter schema changed. Start a compatible baseline; newly extracted fields are not telemetry changes.")
        elif current["source_timestamp_field"] != previous["source_timestamp_field"]:
            baseline_status = "timestamp_basis_changed"
        elif as_time(current["source_timestamp"]) < as_time(previous["source_timestamp"]):
            baseline_status = "out_of_order_telemetry"
        elif current["source_timestamp"] == previous["source_timestamp"]:
            baseline_status = ("source_timestamp_reused_with_changed_metrics"
                               if current["metrics"] != previous["metrics"] else "no_new_telemetry")
        else:
            baseline_status = "compared"
        if baseline_status in ("out_of_order_telemetry", "source_timestamp_reused_with_changed_metrics", "timestamp_basis_changed"):
            finding(baseline_status, "warning", {"previous_source_timestamp": previous["source_timestamp"],
                                                  "current_source_timestamp": current["source_timestamp"]},
                    "Validate telemetry ordering/corrections. These samples do not support a trend calculation.")
        if interval > settings.poll_seconds * 1.5:
            finding("comparison_gap", "info", {"actual_interval_seconds": round(interval)},
                    "A polling gap occurred. Treat changes as covering the actual interval shown.")
    metrics = current["metrics"]
    usable = (freshness == "fresh" and cache_age <= settings.stale_seconds
              and baseline_status not in ("out_of_order_telemetry", "source_timestamp_reused_with_changed_metrics", "timestamp_basis_changed"))
    if baseline_status == "compared" and usable:
        for key in ("ram_used_percent", "storage_used_percent", "cpu_used_percent", "wifi_signal_dbm", "cellular_signal_dbm"):
            if key in metrics and key in previous["metrics"]:
                if key in ("ram_used_percent", "storage_used_percent"):
                    capacity = key.replace("used_percent", "total_bytes")
                    if metrics.get(capacity) != previous["metrics"].get(capacity):
                        finding(capacity + "_changed", "info", {"previous": previous["metrics"].get(capacity),
                                                                "current": metrics.get(capacity)},
                                "Capacity changed; utilization change is not comparable. Validate the device configuration.")
                        continue
                if key.endswith("dbm") and (metrics.get("connection_mode") != previous["metrics"].get("connection_mode")
                                            or current.get("signal_units") != previous.get("signal_units")):
                    continue
                if key.endswith("dbm"):
                    interface, mode = ("wifi", 0) if key.startswith("wifi") else ("cellular", 1)
                    if metrics.get("connection_mode") != mode or previous["metrics"].get("connection_mode") != mode:
                        continue
                    if any(sample["metrics"].get(f"{interface}_connected") is False or
                           sample["metrics"].get(f"{interface}_data_enabled") is False for sample in (previous, current)):
                        continue
                delta = round(metrics[key] - previous["metrics"][key], 3)
                deltas[key] = {"previous": previous["metrics"][key], "current": metrics[key],
                               "delta": delta, "unit": "percentage_points" if key.endswith("percent") else "dBm"}
                if key.endswith("percent") and delta >= settings.thresholds["increase_percentage_points"]:
                    finding(key + "_increase", "warning", deltas[key],
                            "Correlate resource growth with app activity and logs; investigate a sustained increase before remediation.")
    if usable:
        recommendations = {
            "ram_used_percent": "Inspect per-app memory and recent releases. Confirm persistent pressure before proposing an app restart.",
            "storage_used_percent": "Inspect log/cache growth and retention. Propose approved cleanup; do not delete device data automatically.",
            "cpu_used_percent": "Correlate sustained CPU load with workload and process diagnostics; a single sample does not identify a responsible app.",
            "wifi_signal_dbm": "Check access-point distance/interference and compare a known-good network before changing settings.",
            "cellular_signal_dbm": "Check coverage, antenna and carrier. Correlate with disconnections before changing connectivity.",
        }
        for key, action in recommendations.items():
            if key.startswith("wifi") and metrics.get("connection_mode") != 0:
                continue
            if key.startswith("cellular") and metrics.get("connection_mode") != 1:
                continue
            if key.endswith("dbm"):
                interface = "wifi" if key.startswith("wifi") else "cellular"
                if metrics.get(f"{interface}_connected") is False or metrics.get(f"{interface}_data_enabled") is False:
                    continue
            value = metrics.get(key)
            threshold = settings.thresholds[key]
            if value is not None and (value <= threshold if key.endswith("dbm") else value >= threshold):
                finding(key + "_threshold", "warning", {"value": value, "threshold": threshold}, action)
        if metrics.get("connection_mode") == 0 and metrics.get("wifi_connected") is False:
            finding("active_wifi_reports_disconnected", "warning", {"wifi_connected": False},
                    "Verify the active interface, collector timing and current connectivity before changing network settings.")
        if metrics.get("mqtt_disconnected_count", 0) > 0:
            finding("mqtt_reports_disconnected", "warning", {"reported_disconnected": metrics["mqtt_disconnected_count"]},
                    "Check the affected MQTT client and broker diagnostics; connection states are not incident counts.")
    transitions = {}
    if previous and baseline_status == "compared" and usable:
        for key, value in metrics.items():
            prior = previous["metrics"].get(key)
            if prior is None or prior == value:
                continue
            if key.endswith("_raw"):
                transitions[key] = {"previous": prior, "current": value,
                                    "meaning": "raw value/code transition; units or code semantics unverified; no arithmetic delta"}
            elif (isinstance(value, (bool, str)) or key == "connection_mode" or key.startswith("mqtt_")):
                transitions[key] = {"previous": prior, "current": value, "meaning": "reported state change; causality unverified"}
    software = software_comparison(previous, current, now, settings, baseline_status == "compared" and usable)
    if software["apps_status"] in ("app_timestamp_reused_with_changed_inventory", "out_of_order_app_data"):
        finding(software["apps_status"], "info", {}, "Validate application inventory timing; do not infer an install or upgrade event.")
    severity = "warning" if any(f["severity"] == "warning" for f in findings) else "info" if findings else "ok"
    return {
        "device_id": current["device_id"], "evaluated_at": iso(now),
        "current_observed_at": current["observed_at"],
        "previous_observed_at": previous["observed_at"] if previous else None,
        "current_source_timestamp": current["source_timestamp"],
        "previous_source_timestamp": previous["source_timestamp"] if previous else None,
        "source_interval_seconds": (as_time(current["source_timestamp"]) - as_time(previous["source_timestamp"])).total_seconds()
                                   if previous and current["source_timestamp_field"] == previous["source_timestamp_field"] else None,
        "baseline_status": baseline_status, "actual_interval_seconds": interval,
        "source_freshness": freshness, "source_age_seconds": round(source_age),
        "cache_age_seconds": round(cache_age), "severity": severity,
        "deltas": deltas, "reported_transitions": transitions, "software_changes": software, "findings": findings,
        "limitations": "Collected observations only; no backfill or root-cause proof. Raw temperatures, battery level and network-stat units are unverified; transaction latency and crash/process diagnostics are unavailable.",
    }

