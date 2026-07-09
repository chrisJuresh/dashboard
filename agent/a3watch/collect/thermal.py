"""Temperatures / fans / voltages from hwmon (CPU, board, NVMe). Non-waking:
reads /sys/class/hwmon only — HDD temperatures come from the gated storage
collector, never from here."""
from __future__ import annotations
from . import Collector, Metric, Ctx, register, safe


class Thermal(Collector):
    name = "thermal"
    group = "Thermal"

    def collect(self, ctx: Ctx) -> list[Metric]:
        out: list[Metric] = []
        seen: dict = {}
        for r in safe.hwmon():
            # stable, unique key: chip.kindN.label
            base = f"{r['chip']}.{r['label']}".replace(" ", "_")
            n = seen.get(base, 0)
            seen[base] = n + 1
            key = f"{r['kind']}.{base}" + (f".{n}" if n else "")
            # series for the interesting ones: any temperature and any fan
            series = r["kind"] in ("temp", "fan")
            out.append(Metric(key, num=r["value"], unit=r["unit"], series=series,
                             text=f"{r['chip']} {r['label']}"))
        return out


register(Thermal())
