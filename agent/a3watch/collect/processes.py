"""
System-wide process view from /proc (memory-backed; touches no disk).

One cheap pass over /proc reads each process's `stat` (for comm + state) and
`statm` (for resident pages), so we get the total process count, the zombie
count and the top-5 processes by RSS without the expense of parsing every
`status` file. Total thread count is taken straight from /proc/loadavg's task
counter rather than summing per-process threads. Every read tolerates a process
that vanished mid-scan (a race) by skipping it.
"""
from __future__ import annotations
import os
from . import Collector, Metric, Ctx, register, safe

_MB = 1024 * 1024
try:
    _PAGE = os.sysconf("SC_PAGE_SIZE")
except (ValueError, OSError, AttributeError):
    _PAGE = 4096


def _san(comm: str) -> str:
    """Make a process name safe to use inside a dotted metric key."""
    out = "".join(c if (c.isalnum() or c in "-_") else "_" for c in comm)
    return out.strip("_") or "unknown"


def _parse_stat(txt: str):
    """Return (comm, state) from a /proc/<pid>/stat line, or None.

    comm sits between the first '(' and the LAST ')' (it may itself contain
    spaces or parentheses); the state char is the first token after it."""
    lp = txt.find("(")
    rp = txt.rfind(")")
    if lp < 0 or rp < lp:
        return None
    comm = txt[lp + 1:rp]
    rest = txt[rp + 1:].split()
    state = rest[0] if rest else "?"
    return comm, state


class Processes(Collector):
    name = "processes"
    group = "Processes"
    every_cycles = 6  # full /proc walk — slow-changing snapshot, run ~every 2 min

    def collect(self, ctx: Ctx) -> list[Metric]:
        try:
            pids = [e for e in os.listdir("/proc") if e.isdigit()]
        except OSError:
            return []
        if not pids:
            return []

        total = 0
        zombies = 0
        top: list[tuple[int, int, str]] = []  # (rss_bytes, pid, comm)

        for pid in pids:
            st = _parse_stat(safe.read_text(f"/proc/{pid}/stat"))
            if st is None:
                continue  # process gone or unreadable — skip
            total += 1
            comm, state = st
            if state == "Z":
                zombies += 1
            statm = safe.read_text(f"/proc/{pid}/statm").split()
            if len(statm) >= 2:
                try:
                    rss = int(statm[1]) * _PAGE  # resident pages -> bytes
                except ValueError:
                    continue
                if rss:
                    top.append((rss, int(pid), comm))

        out: list[Metric] = [
            Metric("total_processes", num=total, series=True),
            Metric("zombies", num=zombies),
        ]

        # total tasks (processes + threads): denominator of loadavg's "run/total"
        run_total = safe.loadavg()[3]  # e.g. "1/234"
        if "/" in run_total:
            running, _, tasks = run_total.partition("/")
            if tasks.isdigit():
                out.append(Metric("total_threads", num=int(tasks)))
            if running.isdigit():
                out.append(Metric("running", num=int(running)))

        # top 5 by RSS -> proc.topmem.<comm> = MB (disambiguate name collisions)
        top.sort(key=lambda t: t[0], reverse=True)
        used_keys: set[str] = set()
        for rss, pid, comm in top[:5]:
            key = f"proc.topmem.{_san(comm)}"
            if key in used_keys:
                key = f"{key}.{pid}"
            used_keys.add(key)
            out.append(Metric(key, num=round(rss / _MB, 1), unit="MB", text=comm))

        return out


register(Processes())
