"""
Load & scheduler pressure from /proc/loadavg and /proc/stat.

loadavg gives the 1/5/15-minute run-queue averages plus the running/total
process counts. /proc/stat's cumulative counters (ctxt, processes, intr) are
turned into per-second rates via ctx.prev deltas: context-switch, fork and
interrupt rates are what actually pull the CPU out of idle, so they help
explain wakeups. Pure /proc reads — touches no disk.
"""
from __future__ import annotations
from . import Collector, Metric, Ctx, register, safe


def _proc_stat_counters() -> dict:
    """Cumulative-since-boot counters from /proc/stat. Missing lines are simply
    absent from the result (a counter that isn't there just yields no rate)."""
    out: dict = {}
    for line in safe.read_text("/proc/stat").splitlines():
        f = line.split()
        if len(f) < 2:
            continue
        # intr's first field is the total across all IRQ lines — that's what we want.
        if f[0] in ("ctxt", "processes", "intr"):
            try:
                out[f[0]] = int(f[1])
            except ValueError:
                continue
    return out


class Load(Collector):
    name = "load"
    group = "Load"

    def collect(self, ctx: Ctx) -> list[Metric]:
        out: list[Metric] = []

        # ---- /proc/loadavg: run-queue averages + process counts ----
        l1, l5, l15, procs = safe.loadavg()
        out.append(Metric("load1", num=round(l1, 2), series=True))
        out.append(Metric("load5", num=round(l5, 2)))
        out.append(Metric("load15", num=round(l15, 2)))
        if "/" in procs:  # e.g. "2/431" -> running/total
            run_s, _, tot_s = procs.partition("/")
            try:
                out.append(Metric("procs_running", num=int(run_s)))
                out.append(Metric("procs_total", num=int(tot_s)))
            except ValueError:
                pass

        # ---- /proc/stat cumulative counters -> per-second rates ----
        cur = _proc_stat_counters()
        st = ctx.prev.setdefault(self.name, {})
        prev_ts = st.get("ts")
        dt = (ctx.ts - prev_ts) if prev_ts else 0.0
        if dt <= 0:
            dt = ctx.interval_s or 0.0

        # (raw key, emitted rate key, keep time-series history?)
        for key, rate_key, series in (
            ("ctxt", "ctxt_per_s", True),
            ("processes", "forks_per_s", False),
            ("intr", "intr_per_s", True),
        ):
            val = cur.get(key)
            prev_val = st.get(key)
            # Need a previous reading, positive window, and no counter reset
            # (a reboot zeroes these; val < prev_val => skip that cycle's rate).
            if val is not None and prev_val is not None and dt > 0 and val >= prev_val:
                out.append(Metric(rate_key, num=round((val - prev_val) / dt, 1),
                                  unit="/s", series=series))
            if val is not None:
                st[key] = val
        st["ts"] = ctx.ts
        return out


register(Load())
