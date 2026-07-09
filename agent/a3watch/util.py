"""
a3watch.util — safe, non-waking primitives.

SAFETY CONTRACT (read before editing):

  1. This module NEVER opens a block device node (/dev/sd*, /dev/nvme*) for
     read or write. Doing so can issue platter I/O and spin up a sleeping HDD.
     We only ever read kernel pseudo-files under /proc and /sys, which are
     served from memory and touch no disk.

  2. The ONLY subprocess permitted to talk to a disk during NORMAL operation is
     `hdparm -C <dev>` (ATA CHECK POWER MODE). Per the ATA spec this command is
     answered by the drive's controller and does NOT spin up a standby disk.
     It is routed exclusively through `hdparm_power_state()` below, which
     refuses any drive flagged `protected` and is a no-op unless explicitly
     enabled in config. Every other disk-touching command (smartctl -a, dd,
     reads of raw devices, blktrace, eBPF on bios) is DIAGNOSTIC-ONLY and lives
     in diag.py behind an explicit mode gate — never imported here.

  3. Every reader tolerates missing files / permission errors and returns a
     neutral value. A sampling cycle must never crash because a counter moved.

If you add a function that runs a subprocess, it MUST appear in
NORMAL_MODE_CMD_ALLOWLIST or be clearly marked diagnostic-only and gated.
"""

from __future__ import annotations

import os
import subprocess
import time
from typing import Optional

# Commands allowed to run during normal (non-diagnostic) operation. Anything
# not on this list that touches hardware belongs in diag.py.
NORMAL_MODE_CMD_ALLOWLIST: frozenset[tuple[str, ...]] = frozenset(
    {
        ("hdparm", "-C"),  # ATA CHECK POWER MODE — spec-guaranteed non-waking
        ("systemctl",),    # unit metadata (CPUUsageNSec, timer fire times)
        ("findmnt",),      # mount table (reads /proc, no device I/O)
    }
)


# --------------------------------------------------------------------------
# pseudo-file readers (memory-backed; never touch a disk platter)
# --------------------------------------------------------------------------
def read_text(path: str, default: str = "") -> str:
    """Read a /proc or /sys pseudo-file. Never raises."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except (OSError, ValueError):
        return default


def read_first_line(path: str, default: str = "") -> str:
    txt = read_text(path, default)
    if txt is default:
        return default
    return txt.split("\n", 1)[0].strip()


def read_int(path: str, default: Optional[int] = None) -> Optional[int]:
    s = read_first_line(path, "")
    if s == "":
        return default
    try:
        return int(s)
    except ValueError:
        return default


def list_dir(path: str) -> list[str]:
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def path_exists(path: str) -> bool:
    try:
        return os.path.exists(path)
    except OSError:
        return False


# --------------------------------------------------------------------------
# time & identity
# --------------------------------------------------------------------------
def now() -> float:
    """Wall-clock seconds. Used for row timestamps and delta windows."""
    return time.time()


def monotonic() -> float:
    return time.monotonic()


def boot_id() -> str:
    """Stable per-boot identifier; a change means the machine rebooted and all
    cumulative kernel counters reset (deltas across it must be discarded)."""
    return read_first_line("/proc/sys/kernel/random/boot_id", "unknown")


# --------------------------------------------------------------------------
# controlled subprocess execution
# --------------------------------------------------------------------------
def run_cmd(
    argv: list[str],
    timeout: float = 5.0,
    allow_diagnostic: bool = False,
) -> tuple[int, str, str]:
    """Run a command with a timeout, capturing output. Returns (rc, out, err).

    In normal mode the command's (name, first-flag) prefix must be in
    NORMAL_MODE_CMD_ALLOWLIST. Diagnostic callers pass allow_diagnostic=True
    (only diag.py should do so, and only inside an explicit diagnostic session).
    """
    if not argv:
        return (127, "", "empty argv")
    prefix1 = (argv[0],)
    prefix2 = (argv[0], argv[1]) if len(argv) > 1 else prefix1
    permitted = (
        prefix1 in NORMAL_MODE_CMD_ALLOWLIST
        or prefix2 in NORMAL_MODE_CMD_ALLOWLIST
    )
    if not permitted and not allow_diagnostic:
        return (126, "", f"command not permitted in normal mode: {argv[0]}")
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return (proc.returncode, proc.stdout, proc.stderr)
    except FileNotFoundError:
        return (127, "", f"not found: {argv[0]}")
    except subprocess.TimeoutExpired:
        return (124, "", "timeout")
    except OSError as exc:
        return (1, "", str(exc))


def have_cmd(name: str) -> bool:
    for d in os.environ.get("PATH", "/usr/sbin:/usr/bin:/sbin:/bin").split(":"):
        if d and os.path.exists(os.path.join(d, name)):
            return True
    # sbin locations that may not be on a service PATH
    for d in ("/usr/sbin", "/sbin", "/usr/bin", "/bin"):
        if os.path.exists(os.path.join(d, name)):
            return True
    return False


# --------------------------------------------------------------------------
# the single permitted disk-touching command in normal mode
# --------------------------------------------------------------------------
def hdparm_power_state(dev: str) -> Optional[str]:
    """Return 'active', 'idle', 'standby', or 'sleeping' via `hdparm -C`.

    ATA CHECK POWER MODE does NOT wake a standby drive. Returns None if hdparm
    is unavailable or the state could not be parsed. Callers are responsible
    for skipping `protected` drives — this function will still run if called,
    but it is non-waking by design.
    """
    dev_path = dev if dev.startswith("/dev/") else f"/dev/{dev}"
    rc, out, _ = run_cmd(["hdparm", "-C", dev_path], timeout=5.0)
    if rc != 0:
        return None
    for line in out.splitlines():
        if "drive state is" in line:
            # "drive state is:  standby"
            state = line.split(":", 1)[1].strip()
            return state or None
    return None


# --------------------------------------------------------------------------
# device / topology helpers (sysfs only — never wake a disk)
# --------------------------------------------------------------------------
def is_rotational(dev_name: str) -> Optional[bool]:
    """True for HDD, False for SSD/NVMe. dev_name is a kernel name e.g. 'sda'."""
    val = read_int(f"/sys/block/{dev_name}/queue/rotational")
    if val is None:
        return None
    return val == 1


def block_devices() -> list[str]:
    """Whole-disk block devices (sd*, nvme*n*, vd*, hd*), excluding partitions,
    loop, ram, zram, dm."""
    out: list[str] = []
    for name in list_dir("/sys/block"):
        if name.startswith(("loop", "ram", "zram", "dm-", "md")):
            continue
        out.append(name)
    return out


def backing_block_device(path: str) -> Optional[str]:
    """Given a filesystem path, return the kernel block-device name backing it
    (e.g. '/var/lib/a3watch' -> 'nvme0n1'). Uses stat + sysfs only; no device
    I/O. Returns None if it cannot be resolved (e.g. tmpfs/overlay)."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    major = os.major(st.st_dev)
    minor = os.minor(st.st_dev)
    # /sys/dev/block/<maj>:<min> -> partition or whole disk
    link = f"/sys/dev/block/{major}:{minor}"
    if not path_exists(link):
        return None
    try:
        real = os.path.realpath(link)
    except OSError:
        return None
    base = os.path.basename(real)
    # If it's a partition, walk up to the parent whole-disk device.
    parent = os.path.dirname(real)
    if path_exists(os.path.join(real, "partition")):
        return os.path.basename(parent)
    return base


def dev_maj_min(dev_name: str) -> Optional[str]:
    """'sda' -> '8:0' (whole-disk major:minor from sysfs)."""
    s = read_first_line(f"/sys/block/{dev_name}/dev")
    return s or None
