from .analysis import compare
from .errors import error_details
from .metrics import as_time, boundary, iso
from .monitor import poll, utc_now, fetch_provenance
from .settings import normalize_serial_numbers


class Tools:
    def __init__(self, settings, store, clock=utc_now, client=None):
        self.settings, self.store, self.clock, self.client = settings, store, clock, client

    @staticmethod
    def page(offset, limit):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("offset must be nonnegative and limit must be 1–100")

    @staticmethod
    def serials(value):
        return normalize_serial_numbers(value, max_devices=100)

    @staticmethod
    def batch(results):
        for result in results:
            if result.get("error_code"):
                result["error_details"] = error_details(result["error_code"])
        failed = sum(bool(result.get("error_code")) for result in results)
        return {"requested": len(results), "succeeded": len(results) - failed,
                "failed": failed, "devices": results}

    def get_monitor_health(self) -> dict:
        """Read collection activity and retained coverage; serials are supplied at runtime."""
        return {**self.store.health(self.clock(), self.settings.stale_seconds, self.store.device_ids()),
                "poll_seconds": self.settings.poll_seconds, "retention_days": self.settings.retention_days,
                "source": "MST analytics device-details", "audit_is_history": False}

    def list_monitored_devices(self, offset: int = 0, limit: int = 100) -> dict:
        """List serials seen in retained collection attempts, not proof of active monitoring."""
        self.page(offset, limit)
        devices = self.store.device_ids()
        return {"devices": list(devices[offset:offset + limit]), "total": len(devices),
                "next_offset": offset + limit if offset + limit < len(devices) else None}

    def _cached_details(self, serial):
        snapshot = self.store.latest(serial)
        attempt = self.store.latest_attempt(serial)
        failed = attempt and attempt["error_code"]
        result = {"device_id": serial, "snapshot": snapshot, "latest_attempt": attempt,
                  "fetched_from_mst": False, "data_source": "cache" if snapshot else "unavailable",
                  "status": "fetch_failed" if failed else "cached_observation" if snapshot else "awaiting_first_poll"}
        if failed:
            result["error_code"] = attempt["error_code"]
        elif snapshot is None:
            result["error_code"] = "no_cached_observation"
        if result.get("error_code"):
            result["error_details"] = error_details(result["error_code"])
        if snapshot:
            previous = self.store.latest(serial, before=snapshot["observed_at"], baseline=True)
            result["evaluation"] = compare(previous, snapshot, self.settings, self.clock())
        return result

    def _live_reading(self, result):
        """Withhold metrics unless this request obtained a recent online MST reading."""
        result = dict(result)
        snapshot = result.get("snapshot")
        max_age = min(self.settings.poll_seconds, self.settings.stale_seconds)
        code = result.get("error_code")
        if snapshot and result.get("fetched_from_mst") and not code:
            age = (self.clock() - as_time(snapshot["source_timestamp"])).total_seconds()
            result["source_status"] = {
                "is_device_online": snapshot["is_device_online"],
                "source_timestamp": snapshot["source_timestamp"],
                "observed_at": snapshot["observed_at"],
                "source_age_seconds": round(age, 3), "max_source_age_seconds": max_age,
            }
            if snapshot["is_device_online"] is False:
                code = "live_device_offline"
            elif snapshot["is_device_online"] is not True:
                code = "live_status_unknown"
            elif age < 0:
                code = "live_telemetry_future"
            elif age > max_age:
                code = "live_telemetry_stale"
        elif not code:
            code = "live_reading_unavailable"
        result["live_reading_available"] = not bool(code)
        if code:
            # A failed refresh must never expose an older cached snapshot in live-only mode.
            result.update(snapshot=None, status="live_reading_unavailable", error_code=code)
            result.pop("evaluation", None)
        return result

    async def get_device_details(self, serial_numbers: str | list[str], refresh: bool = True,
                                 live_only: bool = False) -> dict:
        """Pass a serial string or array (max 100 unique). No configured serial list required.

        By default GET each device through authenticated MST, cache successful observations
        and compare with the earlier baseline. refresh=false reads cache only. Return a
        devices array in both cases, including per-device failures without losing successes.
        Every refresh=true call requests MST again, even when a cached reading exists.
        Use live_only=true for current online metrics: requires refresh=true, a validated
        MST response, is_device_online=true and a non-future source reading no older than
        min(poll_seconds, stale_seconds), 900 seconds by default. Otherwise metrics and
        comparisons are withheld, with a per-device reason and live_reading_available=false.
        This GET reads Analytics' latest stored telemetry; it cannot wake a device or
        trigger a new collection. fetched_from_mst reports API provenance, not freshness.
        fetched_from_mst is true only for validated MST responses from this call; the
        top-level flag requires all requested devices, with a separate success count.
        This call does not create a recurring schedule; the separate worker takes the same
        serials as command-line parameters for 15-minute polling.
        """
        serials = self.serials(serial_numbers)
        if live_only and not refresh:
            raise ValueError("live_only requires refresh=true; cached observations cannot satisfy an online request")
        collection = None
        if refresh:
            collection = await poll(self.settings, self.store, client=self.client,
                                    clock=self.clock, serial_numbers=serials)
        results = []
        if collection:
            # Return this request's readings, not a parallel request's later cache write.
            for collected in collection["devices"]:
                if collected["status"] == "fetch_failed":
                    result = self._cached_details(collected["device_id"])
                    result.update(status="fetch_failed", error_code=collected["error_code"],
                                  latest_attempt=collected["latest_attempt"],
                                  request_source=collected["request_source"], fetched_from_mst=False)
                else:
                    result = {**collected, "status": "fetched_observation"}
                results.append(result)
        else:
            results = [self._cached_details(serial) for serial in serials]
        if live_only:
            results = [self._live_reading(result) for result in results]
        return {**self.batch(results), **fetch_provenance(results), "refreshed": refresh,
                "live_only": live_only,
                "live_reading_count": sum(result.get("live_reading_available") is True for result in results),
                "live_max_age_seconds": min(self.settings.poll_seconds, self.settings.stale_seconds) if live_only else None,
                "run_id": collection["run_id"] if collection else None}

    def get_device_history(self, serial_numbers: str | list[str], start: str, end: str,
                           timezone_name: str = "Asia/Kolkata", limit: int = 100) -> dict:
        """Read retained polls for one serial or an array, by observation time [start, end).

        Date boundaries mean local midnight. For Oct 22 use start=2026-10-22,
        end=2026-10-23. Each device has its own next_end_exclusive cursor.
        """
        serials = self.serials(serial_numbers)
        first, last = boundary(start, timezone_name), boundary(end, timezone_name)
        return self.batch([self.store.history(serial, first, last, limit) for serial in serials])

    def compare_device_snapshots(self, serial_numbers: str | list[str], previous_observed_at: str,
                                 current_observed_at: str) -> dict:
        """Compare exact retained timestamps for one serial or an array. Missing pairs fail
        per device. For devices with different observation times, call separately using
        their respective history timestamps. Historical freshness uses the selected time.
        """
        serials = self.serials(serial_numbers)
        start, end = boundary(previous_observed_at), boundary(current_observed_at)
        if start >= end:
            raise ValueError("previous_observed_at must precede current_observed_at")
        results = []
        for serial in serials:
            previous, current = self.store.exact(serial, start), self.store.exact(serial, end)
            if not previous or not current:
                results.append({"device_id": serial, "status": "no_history", "error_code": "observation_not_retained"})
            else:
                results.append({**compare(previous, current, self.settings, end), "historical": True,
                                "freshness_evaluated_at": iso(end)})
        return self.batch(results)

    def get_fleet_summary(self, offset: int = 0, limit: int = 50,
                          serial_numbers: str | list[str] | None = None) -> dict:
        """Read cached assessments for runtime-selected serials, or all retained serials.

        Does not fetch MST. Missing observations and failed reads are never healthy.
        """
        self.page(offset, limit)
        serials = self.serials(serial_numbers) if serial_numbers is not None else self.store.device_ids()
        counts = {"total": len(serials), "observed": 0, "missing": 0,
                  "fetch_failed": 0, "stale": 0, "warning": 0, "ok": 0, "info": 0, "unknown": 0}
        results = []
        for index, serial in enumerate(serials):
            result = self._cached_details(serial)
            if result["status"] == "fetch_failed":
                counts["fetch_failed"] += 1
            if result.get("snapshot"):
                counts["observed"] += 1
                evaluation = result["evaluation"]
                if result["status"] == "fetch_failed":
                    counts["unknown"] += 1
                else:
                    counts[evaluation["severity"]] += 1
                if evaluation["source_freshness"] != "fresh" or evaluation["cache_age_seconds"] > self.settings.stale_seconds:
                    counts["stale"] += 1
            else:
                counts["missing"] += 1
                counts["unknown"] += 1
            if offset <= index < offset + limit:
                results.append({k: v for k, v in result.items() if k != "snapshot"})
        return {"evaluated_at": iso(self.clock()), "coverage": counts, "devices": results,
                "next_offset": offset + limit if offset + limit < len(serials) else None,
                "thresholds": self.settings.thresholds,
                "note": "Coverage counts overlap; warnings are evidence for investigation, not proven causes."}

