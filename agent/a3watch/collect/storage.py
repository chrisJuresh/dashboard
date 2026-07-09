"""
Per-disk storage info: power state, filesystem usage, HDD temperature and SMART
health. All disk access is via collect.safe (gated) — a rotational disk that is
not already spun up is skipped entirely, so this never wakes or edits an HDD.
smartctl is throttled (every_cycles) to keep the per-cycle cost tiny.
"""
from __future__ import annotations
from . import Collector, Metric, Ctx, register, safe

_GB = 1000 ** 3


def _smart_attr(out: str, attr_id: str):
    for line in (out or "").splitlines():
        f = line.split()
        if len(f) >= 10 and f[0] == attr_id:
            try:
                return int(f[9].split()[0])
            except (ValueError, IndexError):
                return None
    return None


class Storage(Collector):
    name = "storage"
    group = "Storage"
    every_cycles = 3  # ~60s: SMART reads are subprocesses; awake-only + throttled

    def collect(self, ctx: Ctx) -> list[Metric]:
        out: list[Metric] = []
        for d in ctx.disks:
            if not d.monitored:
                continue
            dev = d.dev
            awake = (not d.rotational) or (dev in ctx.awake_disks)
            out.append(Metric(f"{dev}.role", text=d.role))
            out.append(Metric(f"{dev}.rotational", num=1 if d.rotational else 0))
            out.append(Metric(f"{dev}.awake", num=1 if awake else 0,
                              text="awake" if awake else "asleep"))
            if d.size_bytes:
                out.append(Metric(f"{dev}.size", num=round(d.size_bytes / _GB), unit="GB"))

            # filesystem usage — gated (skipped for a sleeping rotational disk)
            if d.mount:
                fs = safe.statvfs_safe(d.mount, dev, ctx, d.rotational)
                if fs:
                    out.append(Metric(f"{dev}.fs_used_pct", num=fs["pct"], unit="%", series=True))
                    out.append(Metric(f"{dev}.fs_used", num=round(fs["used"] / _GB), unit="GB"))
                    out.append(Metric(f"{dev}.fs_free", num=round(fs["avail"] / _GB), unit="GB"))

            # SMART temperature + health — awake rotational disks only, gated
            if d.rotational and awake:
                sm = safe.smart_awake(dev, ctx)
                if sm:
                    t = safe.smart_temp_c(sm)
                    if t is not None:
                        out.append(Metric(f"{dev}.temp", num=t, unit="C", series=True))
                    for label, aid in (("reallocated", "5"), ("pending", "197"), ("crc_err", "199")):
                        v = _smart_attr(sm, aid)
                        if v is not None:
                            out.append(Metric(f"{dev}.smart.{label}", num=v))
            elif d.rotational:
                out.append(Metric(f"{dev}.temp", num=None, text="asleep — not probed"))
        return out


register(Storage())
