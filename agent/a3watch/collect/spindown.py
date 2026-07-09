"""Per-disk configured spindown timer (from hdparm -S units / hdparm.conf).
Non-waking: reads config files only. Lets the UI say "sda: 2h timer (asleep ✓)"
and "sdc/sdd: no timer — awake by config" instead of crying wolf."""
from __future__ import annotations
from . import Collector, Metric, Ctx, register
from ..disks import detect_spindown_timers


class Spindown(Collector):
    name = "spindown"
    group = "Storage"
    every_cycles = 6  # config rarely changes

    def collect(self, ctx: Ctx) -> list[Metric]:
        timers = detect_spindown_timers()
        out: list[Metric] = []
        for d in ctx.disks:
            if not (d.rotational and d.monitored):
                continue
            t = timers.get(d.dev)
            if t is None:
                out.append(Metric(f"{d.dev}.spindown", text="none configured (won't auto-sleep)"))
            elif t == 0:
                out.append(Metric(f"{d.dev}.spindown", num=0, text="disabled"))
            else:
                out.append(Metric(f"{d.dev}.spindown", num=t, unit="min",
                                  text=f"{t:.0f} min" + (" (2h)" if abs(t - 120) < 1 else "")))
        return out


register(Spindown())
