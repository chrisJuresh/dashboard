"""
Host identity + liveness: uptime, kernel, hostname, boot time, failed systemd
units, logged-in users, and file-descriptor pressure.

Non-waking by construction: every value comes from /proc or /sys (memory-backed,
never touches a platter). The only subprocess is `systemctl --failed`, run through
the gated safe.run() allowlist. No disk access at all — no smart_awake/statvfs here.
Every read tolerates a missing file / permission error and is skipped rather than
raising, so a sampling cycle can never crash on this collector.
"""
from __future__ import annotations

import os
import time

from . import Collector, Metric, Ctx, register, safe

_MAX_UNIT_NAMES = 8  # truncate the failed-unit name list in the text metric


def _human_uptime(secs: float) -> str:
    """'3d 4h 12m' — coarse, human-readable uptime."""
    s = int(secs)
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if d or h:
        parts.append(f"{h}h")
    parts.append(f"{m}m")
    return " ".join(parts)


def _btime() -> int | None:
    """Boot time as epoch seconds, straight from /proc/stat (no arithmetic drift)."""
    for line in safe.read_text("/proc/stat").splitlines():
        if line.startswith("btime "):
            f = line.split()
            if len(f) >= 2:
                try:
                    return int(f[1])
                except ValueError:
                    return None
    return None


def _failed_units() -> list[str] | None:
    """Names of failed systemd units, or None if systemctl is unavailable/failed.
    An empty list means systemctl ran fine and nothing is failed."""
    rc, out, _ = safe.run(["systemctl", "--failed", "--no-legend", "--plain"])
    if rc != 0:
        return None
    names: list[str] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        # drop a leading status glyph (●, *, ×) that some systemd versions print
        if parts and parts[0] in ("●", "*", "×"):
            parts = parts[1:]
        if parts:
            names.append(parts[0])
    return names


def _tty_uids() -> set[int] | None:
    """Distinct owner UIDs of processes that hold a controlling TTY — a cheap
    stand-in for 'logged-in users' with no utmp dependency. None if /proc is
    unreadable. Reads /proc/<pid>/stat only (memory-backed)."""
    try:
        entries = os.listdir("/proc")
    except OSError:
        return None
    uids: set[int] = set()
    for pid in entries:
        if not pid.isdigit():
            continue
        stat = safe.read_text(f"/proc/{pid}/stat")
        if not stat:
            continue
        # comm (field 2) is parenthesised and may contain spaces/parens; split
        # after the LAST ')' so positional fields line up regardless of comm.
        rp = stat.rfind(")")
        if rp == -1:
            continue
        f = stat[rp + 1:].split()
        # after comm: [0]=state [1]=ppid [2]=pgrp [3]=session [4]=tty_nr
        if len(f) < 5:
            continue
        try:
            tty_nr = int(f[4])
        except ValueError:
            continue
        if tty_nr == 0:
            continue  # no controlling terminal
        try:
            uids.add(os.stat(f"/proc/{pid}").st_uid)
        except OSError:
            continue  # process vanished mid-scan — fine
    return uids


class System(Collector):
    name = "system"
    group = "System"

    def collect(self, ctx: Ctx) -> list[Metric]:
        out: list[Metric] = []

        # uptime (seconds) + human text
        up = safe.read_text("/proc/uptime").split()
        if up:
            try:
                uptime_s = float(up[0])
                out.append(Metric("uptime", num=round(uptime_s), unit="s",
                                  text=_human_uptime(uptime_s)))
            except ValueError:
                pass

        # kernel release (compact) + full build/version string
        rel = safe.read_text("/proc/sys/kernel/osrelease").strip()
        if rel:
            out.append(Metric("kernel.release", text=rel))
        ver = safe.read_text("/proc/version").split("\n", 1)[0].strip()
        if ver:
            out.append(Metric("kernel.version", text=ver))

        # hostname
        host = safe.read_text("/proc/sys/kernel/hostname").strip()
        if host:
            out.append(Metric("hostname", text=host))

        # boot time (epoch + local human)
        bt = _btime()
        if bt is not None:
            out.append(Metric("boot_time", num=bt,
                              text=time.strftime("%Y-%m-%d %H:%M:%S",
                                                 time.localtime(bt))))

        # failed systemd units — count is the one series worth graphing
        failed = _failed_units()
        if failed is not None:
            out.append(Metric("failed_units", num=len(failed), series=True))
            if failed:
                shown = failed[:_MAX_UNIT_NAMES]
                text = ", ".join(shown)
                extra = len(failed) - len(shown)
                if extra > 0:
                    text += f" (+{extra} more)"
            else:
                text = "none"
            out.append(Metric("failed_units.list", text=text))

        # logged-in users (distinct UIDs of processes with a TTY)
        uids = _tty_uids()
        if uids is not None:
            out.append(Metric("logged_in_users", num=len(uids)))

        # open file descriptors + system max (/proc/sys/fs/file-nr)
        fn = safe.read_text("/proc/sys/fs/file-nr").split()
        if len(fn) >= 3:
            try:
                fd_open = int(fn[0])
                fd_max = int(fn[2])
            except ValueError:
                fd_open = fd_max = None
            if fd_open is not None and fd_max is not None:
                out.append(Metric("fd_open", num=fd_open))
                out.append(Metric("fd_max", num=fd_max))
                if fd_max:
                    out.append(Metric("fd_used_pct",
                                      num=round(fd_open / fd_max * 100.0, 2),
                                      unit="%"))

        return out


register(System())
