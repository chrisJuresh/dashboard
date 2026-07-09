"""Memory + swap + dirty/writeback pressure (from /proc/meminfo)."""
from __future__ import annotations
from . import Collector, Metric, Ctx, register, safe

_MB = 1024 * 1024


class Memory(Collector):
    name = "memory"
    group = "Memory"

    def collect(self, ctx: Ctx) -> list[Metric]:
        m = safe.meminfo()
        if not m:
            return []
        total = m.get("MemTotal", 0)
        avail = m.get("MemAvailable", 0)
        used = total - avail
        out = [
            Metric("total", num=round(total / _MB), unit="MB"),
            Metric("used", num=round(used / _MB), unit="MB", series=True),
            Metric("available", num=round(avail / _MB), unit="MB"),
            Metric("used_pct", num=round(used / total * 100.0, 1) if total else 0.0, unit="%", series=True),
            Metric("cached", num=round(m.get("Cached", 0) / _MB), unit="MB"),
            Metric("buffers", num=round(m.get("Buffers", 0) / _MB), unit="MB"),
            # dirty/writeback are what eventually become HDD writes — worth watching
            Metric("dirty", num=round(m.get("Dirty", 0) / _MB, 1), unit="MB", series=True),
            Metric("writeback", num=round(m.get("Writeback", 0) / _MB, 1), unit="MB"),
            Metric("slab", num=round(m.get("Slab", 0) / _MB), unit="MB"),
        ]
        swtot = m.get("SwapTotal", 0)
        if swtot:
            swused = swtot - m.get("SwapFree", 0)
            out.append(Metric("swap_total", num=round(swtot / _MB), unit="MB"))
            out.append(Metric("swap_used", num=round(swused / _MB), unit="MB", series=True))
        return out


register(Memory())
