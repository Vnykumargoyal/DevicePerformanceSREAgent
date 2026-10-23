import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import logging
import time
import uuid

from .analysis import compare
from .errors import error_details
from .metrics import as_time, iso
from .mst import MSTClient, FetchError
from .settings import Settings, normalize_serial_numbers
from .store import Store

logger = logging.getLogger(__name__)


def utc_now():
    return datetime.now(timezone.utc)


def fetch_provenance(results):
    fetched = sum(item.get("fetched_from_mst") is True for item in results)
    sources = {item.get("data_source", "unavailable") for item in results}
    return {"fetched_from_mst": bool(results) and fetched == len(results),
            "fetched_from_mst_count": fetched,
            "data_source": next(iter(sources)) if len(sources) == 1 else "mixed"}


@contextmanager
def worker_lock(directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "worker.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("A polling worker already owns this data directory") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


async def poll(settings: Settings, store: Store, client=None, clock=utc_now, serial_numbers=None):
    devices = normalize_serial_numbers(serial_numbers if serial_numbers is not None else settings.devices)
    started = clock()
    run_id = str(uuid.uuid4())
    store.prune(started, settings.retention_days)
    store.start_run(run_id, started)
    semaphore = asyncio.Semaphore(settings.concurrency)
    client = client or MSTClient(settings)
    request_source = getattr(client, "data_source", "injected")

    async def one(serial):
        async with semaphore:
            try:
                snapshot = await client.fetch(serial, clock)
            except FetchError as exc:
                failed_at = clock()
                store.record(run_id, serial, failed_at, error=str(exc))
                return {"device_id": serial, "status": "fetch_failed", "error_code": str(exc),
                        "fetched_from_mst": False, "data_source": "unavailable", "request_source": request_source,
                        "error_details": error_details(str(exc)),
                        "latest_attempt": {"run_id": run_id, "observed_at": iso(failed_at), "error_code": str(exc)}}
            previous = store.latest(serial, before=snapshot["observed_at"], baseline=True)
            if (previous and snapshot["source_timestamp"] == previous["source_timestamp"]
                    and snapshot.get("normalization_version", 1) == previous.get("normalization_version", 1)
                    and snapshot["metrics"] != previous["metrics"]):
                snapshot["data_quality"].append("source_timestamp_reused_with_changed_metrics")
            report = compare(previous, snapshot, settings, clock())
            store.record(run_id, serial, as_time(snapshot["observed_at"]), snapshot=snapshot)
            return {"device_id": serial, "status": "observed", "snapshot": snapshot, "evaluation": report,
                    "fetched_from_mst": request_source == "mst", "data_source": request_source,
                    "request_source": request_source,
                    "latest_attempt": {"run_id": run_id, "observed_at": snapshot["observed_at"], "error_code": None}}

    results = await asyncio.gather(*(one(serial) for serial in devices))
    summary = {"run_id": run_id, "started_at": iso(started), "finished_at": iso(clock()),
               "succeeded": sum(r["status"] == "observed" for r in results),
               "failed": sum(r["status"] == "fetch_failed" for r in results), "devices": results,
               **fetch_provenance(results)}
    store.finish_run(run_id, clock(), {k: v for k, v in summary.items() if k != "devices"})
    return summary


async def run_worker(settings: Settings, once=False):
    if not settings.devices:
        raise ValueError("Pass --serial-numbers SERIAL or --serial-numbers SERIAL1 SERIAL2 to the worker")
    with worker_lock(settings.data_dir):
        store = Store(settings.data_dir)
        while True:
            started = time.monotonic()
            summary = await poll(settings, store)
            if once:
                return summary
            # Only aggregate counts in logs; no raw telemetry, serials or secrets.
            logger.info("Poll completed: succeeded=%d failed=%d", summary["succeeded"], summary["failed"])
            elapsed = time.monotonic() - started
            # Skip missed slots after a slow poll. Never overlap or busy-loop.
            delay = settings.poll_seconds - elapsed % settings.poll_seconds
            if elapsed >= settings.poll_seconds:
                logger.warning("Poll exceeded its interval; skipped missed polling slots")
            await asyncio.sleep(delay)

