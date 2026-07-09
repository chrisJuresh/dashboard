"""Top interrupt sources by rate (/proc/interrupts) — what keeps waking the CPU.

Surfaces the interrupts most responsible for pulling cores out of deep idle
(relevant to C-state residency / power). Pure /proc read: it sums each IRQ line
across all CPUs and uses ctx.prev to turn the cumulative kernel counters into
per-second rates. Written to tolerate the file's ragged formatting — the CPU
header, symbolic rows (NMI/LOC/RES/…), short rows that carry a single count and
no per-CPU breakdown (ERR/MIS), and device labels that themselves contain a
colon such as 'INTC1085:00'.
"""
from __future__ import annotations

from . import Collector, Metric, Ctx, register, safe

_TOP_N = 8


def _parse(text: str) -> tuple[dict, dict]:
    """Parse /proc/interrupts.

    Returns (totals, labels): ident -> summed count across CPUs, and
    ident -> trailing device/handler label. `ident` is the token before the
    first ':' (an IRQ number like '5' or a symbolic mnemonic like 'NMI').
    """
    lines = text.splitlines()
    if not lines:
        return {}, {}
    # Header has one column per CPU ("CPU0 CPU1 ..."); use it as an upper bound
    # on how many leading integer columns to treat as counts. None => no cap.
    ncpu = sum(1 for t in lines[0].split() if t.startswith("CPU")) or None
    totals: dict[str, int] = {}
    labels: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        # Split once only: some labels contain ':' (e.g. 'INTC1085:00').
        ident, rest = line.split(":", 1)
        ident = ident.strip()
        if not ident:
            continue
        toks = rest.split()
        # Leading integer tokens are the per-CPU counts; the first non-integer
        # token (chip type such as 'IR-IO-APIC', or a description word) begins
        # the label. Capped at ncpu so a numeric-looking label token can't be
        # mistaken for a count.
        counts: list[int] = []
        i = 0
        while i < len(toks) and (ncpu is None or i < ncpu):
            try:
                counts.append(int(toks[i]))
            except ValueError:
                break
            i += 1
        if not counts:
            continue
        totals[ident] = sum(counts)
        labels[ident] = " ".join(toks[i:]).strip()
    return totals, labels


class Irq(Collector):
    name = "irq"
    group = "IRQ"

    def collect(self, ctx: Ctx) -> list[Metric]:
        totals, labels = _parse(safe.read_text("/proc/interrupts"))
        if not totals:
            return []

        st = ctx.prev.setdefault(self.name, {})
        prev_totals = st.get("totals") or {}
        prev_ts = st.get("ts")
        # Persist this reading for the next cycle's delta (even if we bail below).
        st["totals"] = totals
        st["ts"] = ctx.ts

        dt = (ctx.ts - prev_ts) if prev_ts else None
        if not dt or dt <= 0:
            dt = ctx.interval_s if (ctx.interval_s and ctx.interval_s > 0) else None
        # First cycle (or no usable window): prime state, emit no rates.
        if not prev_totals or not dt:
            return []

        rates: list[tuple[float, str]] = []
        total_delta = 0
        for ident, cur in totals.items():
            prev = prev_totals.get(ident)
            if prev is None:
                continue
            delta = cur - prev
            if delta < 0:  # counter reset (reboot/overflow) — discard this one
                continue
            total_delta += delta
            rates.append((delta / dt, ident))

        out: list[Metric] = [
            Metric("total", num=round(total_delta / dt, 1), unit="intr/s", series=True),
        ]
        rates.sort(reverse=True)
        for per_sec, ident in rates[:_TOP_N]:
            out.append(Metric(f"irq.{ident}", num=round(per_sec, 1),
                              unit="intr/s", text=labels.get(ident) or ident))
        return out


register(Irq())
