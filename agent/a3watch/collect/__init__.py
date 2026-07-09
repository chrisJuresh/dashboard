"""
a3watch.collect — pluggable, non-waking server-info collectors.

Each collector is a module in this package that registers a Collector instance.
The sampler runs the due collectors once per cycle (no new timers / wakeups) and
stores their Metrics. Adding info = drop in a new module; removing it = delete the
module or clear its enable flag. Everything a collector emits is a Metric; the ones
flagged `series=True` also get history for graphing, the rest are just the latest
snapshot (keeps the DB — on NVMe — bounded).

SAFETY (enforced by construction):
  * Collectors read /proc, /sys, hwmon, and run only non-waking commands.
  * The ONLY way to touch a disk is via collect.safe.smart_awake()/statvfs_safe(),
    which refuse any rotational disk that is not already spun up (ctx.awake_disks).
    So no collector — even a fanned-out one — can wake or edit an HDD.
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Metric:
    key: str                      # e.g. "temp.package"
    num: Optional[float] = None   # numeric value (for series/rollup)
    unit: str = ""                # "C", "W", "MHz", "%", "MB", "B/s", ...
    text: Optional[str] = None    # for non-numeric values (governor, model, ...)
    series: bool = False          # also keep time-series history for this one


@dataclass
class Ctx:
    ts: float
    interval_s: float
    cycle: int                    # monotonically increasing cycle counter
    awake_disks: set              # dev names safe to probe (not standby/sleeping)
    disks: list                   # list[DiskCfg]
    price_gbp_kwh: float          # current London electricity unit price
    budget_gbp_year: float = 5.0  # yearly self-overhead budget target
    prev: dict = field(default_factory=dict)  # per-collector persisted state


class Collector:
    name: str = "base"
    group: str = "Other"          # UI grouping
    every_cycles: int = 1         # run once per N cycles (throttle costly ones)
    enabled_by_default: bool = True

    def collect(self, ctx: Ctx) -> list[Metric]:
        return []


REGISTRY: dict[str, Collector] = {}


def register(c: Collector) -> None:
    REGISTRY[c.name] = c


def load_collectors() -> dict[str, Collector]:
    """Import every collector module so it self-registers. Idempotent."""
    from . import safe  # noqa: F401  (shared helpers, not a collector)
    import a3watch.collect as pkg
    for m in pkgutil.iter_modules(pkg.__path__):
        if m.name in ("safe",):
            continue
        try:
            importlib.import_module(f"a3watch.collect.{m.name}")
        except Exception:
            # a broken collector must never break the agent
            continue
    return REGISTRY
