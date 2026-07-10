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


def _hdparm_s_to_min(n: int):
    """Decode an `hdparm -S <n>` value to minutes. 0 => disabled (never spins down).
    None => couldn't decode."""
    if n == 0:
        return 0.0  # spindown disabled
    if 1 <= n <= 240:
        return round(n * 5 / 60.0, 1)
    if 241 <= n <= 251:
        return float((n - 240) * 30)
    if n == 252:
        return 21.0
    if n == 255:
        return 21.6
    return None


def _strip_comments(text: str) -> str:
    """Drop whole comment lines (leading # or ;) so commented-out example blocks
    in /etc/hdparm.conf or a script are not parsed as live config."""
    return "\n".join(ln for ln in text.splitlines()
                     if not ln.lstrip().startswith(("#", ";")))


def detect_spindown_timers() -> dict:
    """{dev: minutes} for drives with a configured hdparm -S spindown timer.
    minutes==0.0 means spindown is explicitly disabled; a dev absent from the map
    has NO spindown configured (so it will simply stay awake — not an anomaly).
    Non-waking: reads systemd unit files, /etc/hdparm.conf, and resolves symlinks."""
    import glob
    import re
    out: dict = {}
    texts = []
    for f in glob.glob("/etc/systemd/system/*.service") + ["/etc/hdparm.conf"]:
        texts.append(util.read_text(f))
    blob = _strip_comments("\n".join(texts))  # ignore commented-out examples
    # /etc/hdparm.conf blocks: "/dev/xxx { spindown_time = N }"
    for m in re.finditer(r"(/dev/[\w/\-]+)\s*\{[^}]*spindown_time\s*=\s*(\d+)", blob):
        _map_spindown(out, m.group(1), int(m.group(2)))
    # hdparm -S N <device> in unit ExecStart lines
    for m in re.finditer(r"hdparm\s+.*?-S\s+(\d+)\s+(/dev/[\w/\-]+)", blob):
        _map_spindown(out, m.group(2), int(m.group(1)))
    return out


def _map_spindown(out: dict, path: str, nval: int) -> None:
    try:
        dev = os.path.basename(os.path.realpath(path))
    except OSError:
        return
    mins = _hdparm_s_to_min(nval)
    if dev and mins is not None:
        out[dev] = mins


# ---------------------------------------------------- scheduled sleep -------
_UNIT_DIRS = ("/etc/systemd/system", "/run/systemd/system",
              "/lib/systemd/system", "/usr/lib/systemd/system")
_LOCAL_SCRIPT_DIRS = ("/usr/local/sbin", "/usr/local/bin")
# only ExecStart tokens under a real binary/script root are the command; this
# excludes flock lockfiles (/run/*.lock) and other path arguments
_SCRIPT_ROOTS = ("/usr/local/sbin/", "/usr/local/bin/", "/usr/sbin/", "/usr/bin/",
                 "/sbin/", "/bin/", "/opt/")
# tokens in ExecStart that are wrappers, not the script we care about
_EXEC_WRAPPERS = {"flock", "env", "bash", "sh", "nice", "ionice", "timeout", "true"}


def _glob_units(pattern: str) -> list[str]:
    import glob
    out: list[str] = []
    for d in _UNIT_DIRS:
        out += glob.glob(os.path.join(d, pattern))
    return out


def _unit_file(name: str):
    for d in _UNIT_DIRS:
        p = os.path.join(d, name)
        if util.path_exists(p):
            return p
    return None


def _ini_values(text: str, key: str) -> list[str]:
    """Every 'Key=value' value for a systemd directive (last-wins is a systemd
    runtime concern; for detection we want them all)."""
    import re
    return [m.group(1).strip() for m in
            re.finditer(rf"(?im)^\s*{re.escape(key)}\s*=\s*(.+?)\s*$", text)]


def _scripts_from_execstart(text: str) -> set:
    """Absolute script paths named on ExecStart= lines (skipping wrappers)."""
    import re
    out: set = set()
    for val in _ini_values(text, "ExecStart"):
        for tok in re.findall(r"/[\w./\-]+", val.lstrip("-+!@")):
            if os.path.basename(tok) in _EXEC_WRAPPERS:
                continue
            if tok.startswith(_SCRIPT_ROOTS):
                out.add(tok)
    return out


def _referenced_local_scripts(text: str) -> set:
    """Full paths of /usr/local/{sbin,bin} scripts referenced by bare basename in
    `text` — one level of indirection (e.g. a3-maintenance calls a3-hgst-sleep)."""
    import re
    found: set = set()
    for d in _LOCAL_SCRIPT_DIRS:
        for name in util.list_dir(d):
            if re.search(rf"(?<![\w/\-]){re.escape(name)}(?![\w\-])", text):
                found.add(os.path.join(d, name))
    return found


def _force_sleep_devices(text: str) -> list[str]:
    """Kernel dev names put to standby by a force-sleep command in `text`
    (`hdparm -y|-Y <dev>`), resolving both literal /dev paths and simple
    VAR=/dev/... ; hdparm -y "$VAR" indirection used by the a3 sleep scripts."""
    import re
    paths: list[str] = []
    paths += re.findall(r"hdparm\s+[^\n]*?-[yY]\b[^\n]*?(/dev/[\w/\-]+)", text)
    assigns = dict(re.findall(r'(\w+)=["\']?(/dev/[\w/\-]+)', text))
    for var in re.findall(r'hdparm\s+[^\n]*?-[yY]\b[^\n]*?\$\{?(\w+)\}?', text):
        if var in assigns:
            paths.append(assigns[var])
    devs: list[str] = []
    for p in paths:
        try:
            dev = os.path.basename(os.path.realpath(p))
        except OSError:
            continue
        if dev and dev not in devs:
            devs.append(dev)
    return devs


def detect_sleep_schedules() -> dict:
    """{dev: {'unit', 'when', 'how'}} for drives put to sleep by a *scheduled* job:
    a systemd .timer whose service (directly or via a helper script) force-sleeps
    the drive with `hdparm -y/-Y`.

    This is an event on a clock, NOT an idle countdown, so it is reported
    separately from detect_spindown_timers() and must never be treated as a
    'should be asleep by now' idle timer — that distinction is exactly the honesty
    bug this fixes. Non-waking: reads unit files + scripts only, runs nothing."""
    schedules: dict = {}
    for tp in _glob_units("*.timer"):
        ttext = util.read_text(tp)
        whens = (_ini_values(ttext, "OnCalendar")
                 + _ini_values(ttext, "OnUnitActiveSec")
                 + _ini_values(ttext, "OnBootSec"))
        unit = (_ini_values(ttext, "Unit") or [None])[0]
        if not unit:
            unit = os.path.basename(tp)[:-len(".timer")] + ".service"
        sp = _unit_file(unit)
        if not sp:
            continue
        stext = util.read_text(sp)
        scripts = _scripts_from_execstart(stext)
        for s in list(scripts):
            scripts |= _referenced_local_scripts(util.read_text(s))
        blob = _strip_comments(
            stext + "\n" + "\n".join(util.read_text(s) for s in sorted(scripts)))
        when = ", ".join(whens) if whens else "on schedule"
        for dev in _force_sleep_devices(blob):
            schedules.setdefault(dev, {"unit": unit, "when": when,
                                       "how": "unmount + hdparm -y (force standby)"})
    return schedules


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
