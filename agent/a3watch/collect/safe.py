"""
a3watch.collect.safe — shared, non-waking helpers for collectors.

This is the ONLY place a collector may touch a disk, and it refuses to touch any
rotational disk that isn't already spun up. Everything else here reads /proc, /sys
or hwmon (memory-backed; never spins a platter).
"""

from __future__ import annotations

import os
from typing import Optional

from .. import util


# ------------------------------------------------------------ /proc/sys ----
def read_text(path: str) -> str:
    return util.read_text(path)


def read_int(path: str, default: Optional[int] = None):
    return util.read_int(path, default)


def read_float(path: str):
    s = util.read_first_line(path)
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def meminfo() -> dict:
    out = {}
    for line in util.read_text("/proc/meminfo").splitlines():
        parts = line.split(":")
        if len(parts) != 2:
            continue
        k = parts[0].strip()
        v = parts[1].strip().split()
        try:
            out[k] = int(v[0]) * (1024 if len(v) > 1 and v[1] == "kB" else 1)
        except (ValueError, IndexError):
            continue
    return out  # bytes


def loadavg() -> tuple:
    p = util.read_first_line("/proc/loadavg").split()
    try:
        return (float(p[0]), float(p[1]), float(p[2]), p[3] if len(p) > 3 else "")
    except (ValueError, IndexError):
        return (0.0, 0.0, 0.0, "")


def net_dev() -> dict:
    """iface -> {rx_bytes, tx_bytes, rx_pkts, tx_pkts, rx_err, tx_err} (cumulative)."""
    out = {}
    for line in util.read_text("/proc/net/dev").splitlines():
        if ":" not in line:
            continue
        name, rest = line.split(":", 1)
        name = name.strip()
        if name in ("lo",):
            continue
        f = rest.split()
        if len(f) < 16:
            continue
        try:
            out[name] = {"rx_bytes": int(f[0]), "rx_pkts": int(f[1]), "rx_err": int(f[2]),
                         "tx_bytes": int(f[8]), "tx_pkts": int(f[9]), "tx_err": int(f[10])}
        except ValueError:
            continue
    return out


def hwmon() -> list:
    """All hwmon readings. [{chip, label, kind, value, unit}] — temps/fans/volts/power.
    Pure sysfs; touches no disk (NVMe/board/CPU sensors only — HDD temps come from
    smart_awake, gated)."""
    out = []
    base = "/sys/class/hwmon"
    for h in util.list_dir(base):
        d = os.path.join(base, h)
        chip = util.read_first_line(os.path.join(d, "name")) or h
        # CRITICAL: never read the 'drivetemp' chip — its temp*_input issues an ATA
        # temperature command that can spin up a standby HDD. HDD temps come only from
        # the gated smart_awake() path (awake disks only). This preserves non-waking.
        if chip == "drivetemp":
            continue
        for f in util.list_dir(d):
            v = util.read_int(os.path.join(d, f))
            if v is None:
                continue
            lbl_path = os.path.join(d, f.rsplit("_", 1)[0] + "_label")
            label = util.read_first_line(lbl_path) or f.rsplit("_", 1)[0]
            if f.startswith("temp") and f.endswith("_input"):
                out.append({"chip": chip, "label": label, "kind": "temp",
                            "value": round(v / 1000.0, 1), "unit": "C"})
            elif f.startswith("fan") and f.endswith("_input"):
                out.append({"chip": chip, "label": label, "kind": "fan",
                            "value": float(v), "unit": "RPM"})
            elif f.startswith("in") and f.endswith("_input"):
                out.append({"chip": chip, "label": label, "kind": "volt",
                            "value": round(v / 1000.0, 3), "unit": "V"})
            elif f.startswith("power") and f.endswith("_input"):
                out.append({"chip": chip, "label": label, "kind": "power",
                            "value": round(v / 1_000_000.0, 2), "unit": "W"})
    return out


# --------------------------------------------------- non-waking commands ----
def run(argv: list, timeout: float = 6.0) -> tuple:
    """Run a non-waking command. Allowed prefixes only; everything else is refused
    so a collector can't accidentally run something that spins a disk."""
    allowed = (("sensors",), ("smartctl", "-n"), ("systemctl",), ("docker",),
               ("df",), ("findmnt",), ("nproc",))
    if not argv:
        return (127, "", "empty")
    p1 = (argv[0],)
    p2 = (argv[0], argv[1]) if len(argv) > 1 else p1
    if p1 not in allowed and p2 not in allowed:
        return (126, "", f"not permitted: {argv[0]}")
    return util.run_cmd(argv, timeout=timeout, allow_diagnostic=True)


# ------------------------------------------------ gated disk access ONLY ----
def smart_awake(dev: str, ctx) -> Optional[str]:
    """SMART -A output for `dev`, ONLY if it is already spun up. Uses
    `smartctl -n standby` so even a race can't wake a drive that went to standby.
    Returns None for a standby/sleeping/unknown-state rotational disk — so this
    can NEVER wake or edit an HDD."""
    dc = next((d for d in ctx.disks if d.dev == dev), None)
    if dc and dc.rotational and dev not in ctx.awake_disks:
        return None
    if not util.have_cmd("smartctl"):
        return None
    rc, out, _ = run(["smartctl", "-n", "standby", "-A", f"/dev/{dev}"], timeout=6.0)
    # rc bit 1 (value 2) set + standby text => smartctl refused (asleep): treat as None
    if (rc & 2) and ("STANDBY" in out.upper() or "SLEEP" in out.upper()):
        return None
    return out or None


def smart_health(dev: str, ctx) -> Optional[str]:
    """SMART health summary (-H -A), gated identically to smart_awake."""
    dc = next((d for d in ctx.disks if d.dev == dev), None)
    if dc and dc.rotational and dev not in ctx.awake_disks:
        return None
    if not util.have_cmd("smartctl"):
        return None
    rc, out, _ = run(["smartctl", "-n", "standby", "-H", f"/dev/{dev}"], timeout=6.0)
    if (rc & 2) and ("STANDBY" in out.upper() or "SLEEP" in out.upper()):
        return None
    return out or None


def statvfs_safe(mount: str, dev: str, ctx, rotational: bool) -> Optional[dict]:
    """statvfs on a mount, skipped for a rotational disk that isn't spun up (statvfs
    is normally cache-served, but we gate to be certain we never touch a sleeping HDD)."""
    if rotational and dev and dev not in ctx.awake_disks:
        return None
    try:
        s = os.statvfs(mount)
    except OSError:
        return None
    total = s.f_blocks * s.f_frsize
    free = s.f_bfree * s.f_frsize
    avail = s.f_bavail * s.f_frsize
    return {"total": total, "free": free, "avail": avail, "used": total - free,
            "pct": round((total - free) / total * 100.0, 1) if total else 0.0}


def smart_temp_c(smart_out: str) -> Optional[float]:
    """Parse a temperature (C) out of `smartctl -A` output (attr 190/194 or
    a Temperature_ line)."""
    for line in (smart_out or "").splitlines():
        f = line.split()
        if len(f) >= 10 and f[0] in ("190", "194"):
            try:
                return float(f[9].split()[0])
            except (ValueError, IndexError):
                pass
        low = line.lower()
        if "temperature" in low and "celsius" in low:
            for tok in f:
                if tok.isdigit():
                    return float(tok)
    return None
