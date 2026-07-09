"""
a3watch.power — wattage and C-state residency.

All sources here are memory/register reads; none touch a disk:
  * RAPL energy from /sys/class/powercap (root-readable) -> per-domain watts.
  * Core C-state residency from /sys/.../cpuidle (unprivileged, cumulative).
  * Package C-state residency from model-specific MSRs via /dev/cpu/0/msr
    (root; the same registers turbostat reads — cheap, non-disk). If the msr
    device is absent we report no package residency and say so honestly
    (diagnostic-mode turbostat can fill the gap).
  * CPU busy% from /proc/stat.

Everything is expressed as raw cumulative readings + a residency() function that
diffs two readings over dt, so the short-lived sampler can persist "last" and
compute deltas next cycle.
"""

from __future__ import annotations

import os
import struct
from typing import Optional

from . import util

# Intel package C-state residency MSRs (increment at TSC rate).
_MSR_TSC = 0x10
_PKG_MSRS = {
    "PC2": 0x60D,
    "PC3": 0x3F8,
    "PC6": 0x3F9,
    "PC7": 0x3FA,
    "PC8": 0x630,
    "PC9": 0x631,
    "PC10": 0x632,
}
_MSR_DEV = "/dev/cpu/0/msr"

_RAPL_ROOT = "/sys/class/powercap"


# --------------------------------------------------------------- RAPL -------
def read_rapl_energy() -> dict[str, tuple[int, int]]:
    """domain -> (energy_uj, max_energy_range_uj). Root-only; {} if unreadable."""
    out: dict[str, tuple[int, int]] = {}
    for entry in util.list_dir(_RAPL_ROOT):
        if not entry.startswith("intel-rapl:"):
            continue
        base = os.path.join(_RAPL_ROOT, entry)
        name = util.read_first_line(os.path.join(base, "name"))
        if not name:
            continue
        energy = util.read_int(os.path.join(base, "energy_uj"))
        rng = util.read_int(os.path.join(base, "max_energy_range_uj"), 0)
        if energy is None:
            continue  # not readable (non-root) — skip silently
        domain = "package" if name.startswith("package") else name
        # de-dup on the first occurrence of a domain name
        if domain not in out:
            out[domain] = (energy, rng or 0)
    return out


def rapl_watts(
    prev: dict[str, tuple[int, int]],
    cur: dict[str, tuple[int, int]],
    dt: float,
) -> dict[str, float]:
    if dt <= 0:
        return {}
    watts: dict[str, float] = {}
    for domain, (e1, rng) in cur.items():
        if domain not in prev:
            continue
        e0 = prev[domain][0]
        de = e1 - e0
        if de < 0:  # counter wrapped
            de += rng if rng else 0
            if de < 0:
                continue
        watts[domain] = (de / 1_000_000.0) / dt
    return watts


# ------------------------------------------------------ core C-states -------
def read_core_cstates() -> dict:
    """{'ncpu': n, 'time': {state_name: summed_time_us}} from cpuidle sysfs."""
    base = "/sys/devices/system/cpu"
    times: dict[str, int] = {}
    ncpu = 0
    for cpu in util.list_dir(base):
        if not (cpu.startswith("cpu") and cpu[3:].isdigit()):
            continue
        idle = os.path.join(base, cpu, "cpuidle")
        if not util.path_exists(idle):
            continue
        ncpu += 1
        for st in util.list_dir(idle):
            if not st.startswith("state"):
                continue
            name = util.read_first_line(os.path.join(idle, st, "name")) or st
            t = util.read_int(os.path.join(idle, st, "time"))  # microseconds
            if t is None:
                continue
            times[name] = times.get(name, 0) + t
    return {"ncpu": ncpu, "time": times}


def core_cstate_residency(prev: dict, cur: dict, dt: float) -> list[tuple[str, float]]:
    ncpu = cur.get("ncpu", 0) or 1
    if dt <= 0:
        return []
    denom = dt * 1_000_000.0 * ncpu
    out: list[tuple[str, float]] = []
    ptime = prev.get("time", {})
    for name, t1 in cur.get("time", {}).items():
        t0 = ptime.get(name)
        if t0 is None:
            continue
        d = t1 - t0
        if d < 0:
            continue
        out.append((name, max(0.0, min(100.0, d / denom * 100.0))))
    return out


# --------------------------------------------------- package C-states -------
def _read_msr(fd: int, addr: int) -> Optional[int]:
    try:
        raw = os.pread(fd, 8, addr)
        if len(raw) != 8:
            return None
        return struct.unpack("<Q", raw)[0]
    except OSError:
        return None


def read_pkg_cstates() -> dict:
    """{'tsc': int, 'res': {state: counter}} read from MSRs on cpu0.
    Returns {} if /dev/cpu/0/msr is unavailable (module not loaded)."""
    if not util.path_exists(_MSR_DEV):
        return {}
    try:
        fd = os.open(_MSR_DEV, os.O_RDONLY)
    except OSError:
        return {}
    try:
        tsc = _read_msr(fd, _MSR_TSC)
        if tsc is None:
            return {}
        res: dict[str, int] = {}
        for name, addr in _PKG_MSRS.items():
            v = _read_msr(fd, addr)
            if v is not None:
                res[name] = v
        return {"tsc": tsc, "res": res}
    finally:
        os.close(fd)


def pkg_cstate_residency(prev: dict, cur: dict) -> list[tuple[str, float]]:
    """Residency % = Δcounter / Δtsc * 100 (counters tick at TSC rate)."""
    if not prev or not cur:
        return []
    t0, t1 = prev.get("tsc"), cur.get("tsc")
    if not t0 or not t1 or t1 <= t0:
        return []
    dtsc = t1 - t0
    out: list[tuple[str, float]] = []
    pres = prev.get("res", {})
    for name, c1 in cur.get("res", {}).items():
        c0 = pres.get(name)
        if c0 is None:
            continue
        d = c1 - c0
        if d < 0:
            continue
        out.append((name, max(0.0, min(100.0, d / dtsc * 100.0))))
    return out


def pkg_cstates_available() -> bool:
    return util.path_exists(_MSR_DEV)


def core_cstate_info() -> dict:
    """What core C-states the platform actually exposes. If the deepest is only
    C3, deep package idle (PC6+) is *impossible* here regardless of load — a
    BIOS/kernel matter, not a busy process."""
    base = "/sys/devices/system/cpu/cpu0/cpuidle"
    names = []
    for st in sorted(util.list_dir(base)):
        if st.startswith("state"):
            n = util.read_first_line(os.path.join(base, st, "name"))
            if n:
                names.append(n)
    deep = any(any(t in n.upper() for t in ("C6", "C7", "C8", "C9", "C10")) for n in names)
    return {"names": names, "deepest": names[-1] if names else "", "deep_available": deep}


# ------------------------------------------------------------- busy% --------
def read_cpu_busy() -> dict:
    """Raw jiffies from the aggregate /proc/stat cpu line."""
    line = util.read_first_line("/proc/stat")
    if not line.startswith("cpu "):
        # read full file, first line
        for l in util.read_text("/proc/stat").splitlines():
            if l.startswith("cpu "):
                line = l
                break
    parts = line.split()
    # user nice system idle iowait irq softirq steal guest guest_nice
    fields = ["user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal"]
    vals: dict[str, int] = {}
    for i, f in enumerate(fields):
        try:
            vals[f] = int(parts[i + 1])
        except (IndexError, ValueError):
            vals[f] = 0
    return vals


def cpu_busy_pct(prev: dict, cur: dict) -> tuple[float, float, float]:
    """(busy%, iowait%, irq%) between two /proc/stat readings."""
    if not prev or not cur:
        return (0.0, 0.0, 0.0)
    total0 = sum(prev.values())
    total1 = sum(cur.values())
    dt = total1 - total0
    if dt <= 0:
        return (0.0, 0.0, 0.0)
    idle = (cur.get("idle", 0) - prev.get("idle", 0)) + (
        cur.get("iowait", 0) - prev.get("iowait", 0)
    )
    iowait = cur.get("iowait", 0) - prev.get("iowait", 0)
    irq = (cur.get("irq", 0) - prev.get("irq", 0)) + (
        cur.get("softirq", 0) - prev.get("softirq", 0)
    )
    busy = 100.0 * (dt - idle) / dt
    return (
        max(0.0, min(100.0, busy)),
        max(0.0, min(100.0, 100.0 * iowait / dt)),
        max(0.0, min(100.0, 100.0 * irq / dt)),
    )
