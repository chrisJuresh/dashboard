"""
a3watch.disks — per-disk activity and power state, strictly non-waking.

Activity comes from /proc/diskstats (pure kernel counters; reading them never
issues I/O to a drive). Power state comes from sysfs runtime-PM (passive) and,
only for non-`protected` drives when enabled, `hdparm -C` (ATA CHECK POWER
MODE, which the ATA spec guarantees does not spin a drive up). We never open a
device node and never issue SMART or any other command here.
"""

from __future__ import annotations

import os

from . import util
from .config import Config


# ---------------------------------------------------------- diskstats -------
def read_diskstats() -> dict[str, dict]:
    """Whole-disk rows only. {dev: {reads,rsect,writes,wsect,io_ms}}."""
    out: dict[str, dict] = {}
    for line in util.read_text("/proc/diskstats").splitlines():
        parts = line.split()
        if len(parts) < 14:
            continue
        name = parts[2]
        # whole disks appear as directories under /sys/block; partitions don't
        if not util.path_exists(f"/sys/block/{name}"):
            continue
        try:
            out[name] = {
                "reads": int(parts[3]),
                "rsect": int(parts[5]),
                "writes": int(parts[7]),
                "wsect": int(parts[9]),
                "io_ms": int(parts[12]),
            }
        except (ValueError, IndexError):
            continue
    return out


def diskstats_delta(prev: dict[str, dict], cur: dict[str, dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for dev, c in cur.items():
        p = prev.get(dev)
        if not p:
            continue
        d = {}
        ok = True
        for k in ("reads", "rsect", "writes", "wsect", "io_ms"):
            dv = c[k] - p.get(k, 0)
            if dv < 0:  # counter reset (reboot etc.) — discard this dev's delta
                ok = False
                break
            d[k] = dv
        if ok:
            out[dev] = d
    return out


# -------------------------------------------------------- power state -------
def passive_power_state(dev: str) -> str:
    """State from sysfs only, without any command. For SATA HDDs the kernel's
    runtime-PM status is usually 'active' even when the platters are spun down,
    so we return 'standby' only when we can be sure ('suspended'), otherwise
    'unknown' — we do not guess 'active' and imply the disk is spinning."""
    rs = util.read_first_line(f"/sys/block/{dev}/device/power/runtime_status")
    if rs == "suspended":
        return "standby"
    # A drive that has been offlined
    state = util.read_first_line(f"/sys/block/{dev}/device/state")
    if state == "offline":
        return "standby"
    return "unknown"


def power_state(cfg: Config, dev: str) -> str:
    """Authoritative-where-allowed, always non-waking.

    Uses `hdparm -C` only when the tool is configured for it AND this drive is
    not marked `protected`. Falls back to the passive sysfs read otherwise, or
    if hdparm is unavailable / unparsable."""
    dc = cfg.disk(dev)
    protected = dc.protected if dc else True
    if cfg.use_hdparm_c and not protected and util.have_cmd("hdparm"):
        st = util.hdparm_power_state(dev)
        if st:
            # normalise hdparm phrasing
            st = st.strip().lower()
            if "standby" in st:
                return "standby"
            if "sleep" in st:
                return "sleeping"
            if "idle" in st:
                return "idle"
            if "active" in st:
                return "active"
            return st
    return passive_power_state(dev)
