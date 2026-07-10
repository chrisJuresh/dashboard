"""Per-disk sleep policy: ATA idle timer (hdparm -S) AND scheduled force-sleep
(a systemd .timer that unmounts + `hdparm -y` a drive). Non-waking: reads config
files only. Distinguishes three honest cases instead of crying wolf:
  * idle timer      — "sleeps after 120 min" (sda / WDC)
  * scheduled sleep — "force-slept by a3-maintenance @ 04:00" (backup / parity)
  * no policy       — "active pool member, awake by design" (media drives)
Only an idle timer feeds the stay_awake anomaly check; a schedule is a clock
event, not a countdown that can be 'overdue'."""
from __future__ import annotations
from . import Collector, Metric, Ctx, register
from ..disks import detect_spindown_timers, detect_sleep_schedules


class Spindown(Collector):
    name = "spindown"
    group = "Storage"
    every_cycles = 6  # config rarely changes

    def collect(self, ctx: Ctx) -> list[Metric]:
        timers = detect_spindown_timers()
        schedules = detect_sleep_schedules()
        out: list[Metric] = []
        for d in ctx.disks:
            if not (d.rotational and d.monitored):
                continue
            t = timers.get(d.dev)
            sched = schedules.get(d.dev)
            if t is not None and t > 0:
                out.append(Metric(f"{d.dev}.spindown", num=t, unit="min",
                                  text=f"idle timer: sleeps after {t:.0f} min"
                                       + (" (2h)" if abs(t - 120) < 1 else "")))
            elif t == 0:
                out.append(Metric(f"{d.dev}.spindown", num=0, text="idle timer disabled"))
            elif sched:
                out.append(Metric(f"{d.dev}.spindown",
                                  text=f"scheduled sleep — {sched['unit']} ({sched['when']})"))
            else:
                out.append(Metric(f"{d.dev}.spindown",
                                  text="no sleep policy — active pool member, awake by design"))
            # expose the schedule as its own metric so the UI can show the mechanism
            if sched:
                out.append(Metric(f"{d.dev}.sleep_schedule",
                                  text=f"{sched['unit']} · {sched['when']} · {sched['how']}"))
        return out


register(Spindown())
