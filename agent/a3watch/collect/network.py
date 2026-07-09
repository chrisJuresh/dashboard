"""
Network throughput + connection info. Pure /proc: per-interface counters come
from /proc/net/dev (via safe.net_dev, which already skips 'lo'), and the TCP
connection count from /proc/net/tcp{,6}. No disk, no subprocess — nothing here
can wake or edit a drive.

Rates are derived from the delta of the kernel's cumulative byte counters over
ctx.interval_s, using ctx.prev to remember the previous reading. The first cycle
(and any interface that has no previous sample, e.g. after a reboot resets the
counters) has no delta, so its rate is skipped and only cumulative totals are
emitted.
"""
from __future__ import annotations
from . import Collector, Metric, Ctx, register, safe

_GB = 1000 ** 3


def _tcp_conns() -> tuple[int, bool]:
    """Established+listening sockets = data lines (total lines minus header) in
    /proc/net/tcp and /proc/net/tcp6. Returns (count, any_file_readable)."""
    total = 0
    seen = False
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        txt = safe.read_text(path)
        lines = [ln for ln in txt.splitlines() if ln.strip()]
        if not lines:
            continue
        seen = True
        total += max(0, len(lines) - 1)  # drop the header row
    return total, seen


class Network(Collector):
    name = "network"
    group = "Network"
    every_cycles = 1

    def collect(self, ctx: Ctx) -> list[Metric]:
        out: list[Metric] = []
        cur = safe.net_dev()  # iface -> cumulative counters; 'lo' already skipped
        prev = ctx.prev.setdefault(self.name, {})
        iv = ctx.interval_s if ctx.interval_s and ctx.interval_s > 0 else 0.0

        total_rx_bps = 0.0
        total_tx_bps = 0.0
        have_rate = False

        for iface in sorted(cur):
            c = cur[iface]
            rx_bytes = c["rx_bytes"]
            tx_bytes = c["tx_bytes"]

            # cumulative volume + error counters (snapshot, not graphed)
            out.append(Metric(f"{iface}.rx_total", num=round(rx_bytes / _GB, 3), unit="GB"))
            out.append(Metric(f"{iface}.tx_total", num=round(tx_bytes / _GB, 3), unit="GB"))
            out.append(Metric(f"{iface}.rx_err", num=c["rx_err"]))
            out.append(Metric(f"{iface}.tx_err", num=c["tx_err"]))

            # per-interface rate from the delta of cumulative bytes
            p = prev.get(iface)
            if p is not None and iv:
                d_rx = rx_bytes - p["rx_bytes"]
                d_tx = tx_bytes - p["tx_bytes"]
                # negative delta => counter reset (reboot/wrap): skip this cycle
                if d_rx >= 0 and d_tx >= 0:
                    rx_bps = d_rx / iv
                    tx_bps = d_tx / iv
                    out.append(Metric(f"{iface}.rx_bps", num=round(rx_bps, 1), unit="B/s"))
                    out.append(Metric(f"{iface}.tx_bps", num=round(tx_bps, 1), unit="B/s"))
                    total_rx_bps += rx_bps
                    total_tx_bps += tx_bps
                    have_rate = True

            prev[iface] = {"rx_bytes": rx_bytes, "tx_bytes": tx_bytes}

        # drop remembered ifaces that vanished, so prev doesn't grow unbounded
        for gone in [k for k in prev if k not in cur]:
            del prev[gone]

        # totals are the graph-worthy series
        if have_rate:
            out.append(Metric("rx_bps", num=round(total_rx_bps, 1), unit="B/s", series=True))
            out.append(Metric("tx_bps", num=round(total_tx_bps, 1), unit="B/s", series=True))

        conns, seen = _tcp_conns()
        if seen:
            out.append(Metric("tcp_conns", num=conns))

        return out


register(Network())
