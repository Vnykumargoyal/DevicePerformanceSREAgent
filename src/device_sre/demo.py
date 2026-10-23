"""Synthetic fixtures only: no MST access, no real serial numbers."""
from datetime import timedelta
from dataclasses import replace
import uuid

from .metrics import normalize, iso
from .monitor import poll, utc_now
from .store import Store
from .tools import Tools


def payload(serial, now, ram=45, storage=60, wifi=-55):
    return {"data": {"device_details": {
        "device_id": serial, "timestamp": iso(now), "last_updated": iso(now), "is_device_online": True,
        "device_metrics": {"connectionmode": 0,
                           "ram": {"usedbytes": {"$numberLong": str(ram * 10)}, "totalbytes": 1000},
                           "storage": {"usedbytes": storage * 100, "totalbytes": 10000},
                           "battery": {"batteryerror": 0}, "printererror": 0},
        "wifi_metrics": {"signalstrength": wifi},
        "location": {"latitude": 1, "longitude": 1}}, "app_data": {"ignored": "not retained"}}}


async def run_demo(settings):
    settings = replace(settings, devices=("DEMO-TERMINAL-001", "DEMO-TERMINAL-002"),
                       data_dir=settings.data_dir / "synthetic-demo" / uuid.uuid4().hex)
    store = Store(settings.data_dir)
    now = utc_now()
    previous = now - timedelta(minutes=15)

    class DemoClient:
        def __init__(self, degraded=False):
            self.degraded = degraded

        async def fetch(self, serial, clock):
            changed = self.degraded and serial == "DEMO-TERMINAL-001"
            return normalize(payload(serial, clock(), ram=92 if changed else 45,
                                     storage=91 if changed else 60, wifi=-82 if changed else -55), serial, clock())

    await poll(settings, store, DemoClient(), clock=lambda: previous)
    await poll(settings, store, DemoClient(True), clock=lambda: now)
    return {"synthetic_data_only": True, "data_directory": str(settings.data_dir),
            "summary": Tools(settings, store, clock=lambda: now).get_fleet_summary()}

