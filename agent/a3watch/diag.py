"""
a3watch.diag — diagnostic mode (explicit, gated, may add overhead).

Nothing here runs unless a user starts a session via the authenticated
/api/diag/start route. Each session is a time-boxed, detached process whose
output is captured to a file under <data_dir>/diag, so it survives the
socket-activated API idle-exiting. This is the ONLY module permitted to run
heavy / disk-touching tools, and the only one that can wake a disk — and that
single case (`smartctl -a`) demands an explicit second `confirm_wake=True`.
"""

from __future__ import annotations

import json
import os
import subprocess
import time

from . import util
from .config import Config


class DiagError(Exception):
    pass


# tool -> builder(seconds, dev) returning argv, plus flags
def _biosnoop_bin() -> str | None:
    for b in ("biosnoop-bpfcc", "biosnoop"):
        if util.have_cmd(b):
            return b
    return None


def _ext4slower_bin() -> str | None:
    for b in ("ext4slower-bpfcc", "ext4slower"):
        if util.have_cmd(b):
            return b
    return None


_BPFTRACE_BIO = (
    'tracepoint:block:block_rq_issue '
    '{ printf("%s pid=%d comm=%s dev=%d:%d sect=%d len=%d\\n", strftime("%H:%M:%S", nsecs), '
    'pid, comm, args->dev >> 20, args->dev & 0xfffff, args->sector, args->nr_sector); }'
)


def _build(tool: str, seconds: int, dev: str | None, confirm_wake: bool) -> tuple[list[str], bool]:
    """Return (argv, wakes_disk). Raises DiagError if unavailable/unsafe."""
    seconds = max(1, min(seconds, 300))
    if tool == "biosnoop":
        b = _biosnoop_bin()
        if not b:
            raise DiagError("biosnoop (bpfcc-tools) is not installed")
        return (["timeout", str(seconds), b], False)
    if tool == "ext4slower":
        b = _ext4slower_bin()
        if not b:
            raise DiagError("ext4slower (bpfcc-tools) is not installed")
        return (["timeout", str(seconds), b, "0"], False)
    if tool == "bpftrace_bio":
        if not util.have_cmd("bpftrace"):
            raise DiagError("bpftrace is not installed")
        return (["timeout", str(seconds), "bpftrace", "-e", _BPFTRACE_BIO], False)
    if tool == "blktrace":
        if not dev:
            raise DiagError("blktrace requires a device")
        if not util.have_cmd("btrace"):
            raise DiagError("blktrace/btrace is not installed")
        # btrace traces block events; it does not issue platter I/O itself.
        return (["timeout", str(seconds), "btrace", f"/dev/{dev}"], False)
    if tool == "turbostat":
        if not util.have_cmd("turbostat"):
            raise DiagError("turbostat is not installed")
        return (["timeout", str(seconds), "turbostat", "--quiet", "--interval", "1"], False)
    if tool == "powertop":
        if not util.have_cmd("powertop"):
            raise DiagError("powertop is not installed")
        return (["timeout", str(seconds + 5), "powertop", f"--time={seconds}", "--csv=/dev/stdout"], False)
    if tool == "smart":
        if not dev:
            raise DiagError("smart requires a device")
        if not confirm_wake:
            raise DiagError(
                "smartctl -a can spin up a standby disk; resend with confirm_wake=true to proceed"
            )
        if not util.have_cmd("smartctl"):
            raise DiagError("smartctl is not installed")
        return (["smartctl", "-a", f"/dev/{dev}"], True)
    raise DiagError(f"unknown tool: {tool}")


_AUDIT_KEY = "a3watch_wake"


def _audit_targets(cfg: Config) -> tuple[list[str], list[str]]:
    """(mount subtrees, device nodes) of monitored rotational disks that are
    currently mounted / present. Resolving these paths is a stat only — it does
    not open a block device, so arming the audit does not wake a disk."""
    mounts, devices = [], []
    for d in cfg.disks:
        if not (d.rotational and d.monitored):
            continue
        node = f"/dev/{d.dev}"
        if os.path.exists(node):
            devices.append(node)
        if d.mount and os.path.ismount(d.mount):
            mounts.append(d.mount)
    return mounts, devices


def _build_audit(cfg: Config, seconds: int) -> tuple[list[str], bool]:
    """Time-boxed auditd capture of file/device access to the monitored HDDs.
    Reveals the process behind a wake at the *syscall* level — including SMB/NFS
    serving and metadata access that leaves little or no block I/O for the
    cheap always-on signals to see."""
    seconds = max(5, min(seconds, 300))
    for b in ("auditctl", "ausearch"):
        if not util.have_cmd(b):
            raise DiagError(f"{b} not found — install the 'auditd' package")
    rc, out, _ = util.run_cmd(["systemctl", "is-active", "auditd"], timeout=5.0)
    if out.strip() != "active":
        raise DiagError("auditd is not running (start it: sudo systemctl start auditd)")
    mounts, devices = _audit_targets(cfg)
    if not mounts and not devices:
        raise DiagError("no monitored, mounted rotational disks to audit")
    # syscall rules (not -w watches) → each deletes precisely with -d; we NEVER
    # use -D, so any pre-existing audit rules (e.g. a3-wake-audit-rules) are untouched.
    specs = [f"-F dir={m} -F perm=rwa" for m in mounts]
    specs += [f"-F path={n} -F perm=rwa" for n in devices]
    add = "\n".join(f'auditctl -a always,exit {s} -k {_AUDIT_KEY} 2>/dev/null' for s in specs)
    dele = "\n".join(f'auditctl -d always,exit {s} -k {_AUDIT_KEY} 2>/dev/null' for s in specs)
    script = f"""set -u
cleanup() {{
{dele}
}}
trap cleanup EXIT INT TERM
{dele}
{add}
echo "# a3watch wake-audit — {seconds}s"
echo "# device-node opens (catches SMART/smartctl/dd raw-device access): {', '.join(devices) or '(none)'}"
echo "# directory-level access at mount roots (listings/browse/creates): {', '.join(mounts) or '(none)'}"
echo "# For deep per-file reads, use the ext4slower / biosnoop (eBPF) tools instead."
echo "# NOTE: 'comm=hdparm' entries are a3watch's own non-waking power-state check — ignore them."
echo
sleep {seconds}
echo "=== processes that accessed these disks (by access count) ==="
ausearch -k {_AUDIT_KEY} -ts recent -i 2>/dev/null | grep '^type=SYSCALL' | grep -vE 'syscall=sendto|comm=(auditctl|auditd|hdparm)' | grep -oE 'comm=[^ ]+' | sed 's/comm=//;s/\"//g' | sort | uniq -c | sort -rn | head -25
echo
echo "=== files / devices touched (count : path) ==="
ausearch -k {_AUDIT_KEY} -ts recent -i 2>/dev/null | grep '^type=PATH' | grep -oE 'name=[^ ]+' | sed 's/name=//;s/\"//g' | grep -E '^/mnt/|^/dev/sd' | sort | uniq -c | sort -rn | head -50
echo
echo "# If both lists are empty, nothing touched these disks during the window —"
echo "# they are staying spun up on their own (e.g. no spindown timer set), not being"
echo "# woken by ongoing access. Run a longer window to catch periodic (SMB/scan) access."
"""
    # timeout sends SIGTERM past a hard deadline so the trap cleanup always runs
    return (["timeout", "--signal=TERM", str(seconds + 25), "bash", "-lc", script], False)


def _diag_dir(cfg: Config) -> str:
    os.makedirs(cfg.diag_dir, exist_ok=True)
    return cfg.diag_dir


def _meta_path(cfg: Config, sid: str) -> str:
    return os.path.join(cfg.diag_dir, f"{sid}.json")


def _out_path(cfg: Config, sid: str) -> str:
    return os.path.join(cfg.diag_dir, f"{sid}.out")


def start(cfg: Config, tool: str, seconds: int, dev: str | None, confirm_wake: bool) -> str:
    _diag_dir(cfg)
    if tool == "audit":
        argv, wakes = _build_audit(cfg, seconds)
    else:
        argv, wakes = _build(tool, seconds, dev, confirm_wake)
    sid = f"{int(time.time())}-{os.urandom(3).hex()}"
    out = _out_path(cfg, sid)
    started = time.time()
    ends = started + min(max(seconds, 1), 300) + 6
    meta = {"id": sid, "tool": tool, "dev": dev, "started": started, "ends": ends,
            "wakes_disk": wakes, "argv": argv}
    with open(_meta_path(cfg, sid), "w") as fh:
        json.dump(meta, fh)
    # detached: survives API idle-exit; captures output to the .out file
    with open(out, "wb") as ofh:
        subprocess.Popen(argv, stdout=ofh, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    return sid


def _sessions(cfg: Config) -> list[dict]:
    out: list[dict] = []
    for name in util.list_dir(cfg.diag_dir):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(cfg.diag_dir, name)) as fh:
                m = json.load(fh)
            m["running"] = time.time() < m.get("ends", 0)
            out.append(m)
        except (OSError, json.JSONDecodeError):
            continue
    out.sort(key=lambda m: m.get("started", 0), reverse=True)
    return out


def is_running(cfg: Config) -> bool:
    return any(s.get("running") for s in _sessions(cfg))


def status(cfg: Config) -> dict:
    s = _sessions(cfg)
    return {
        "running": any(x["running"] for x in s),
        "sessions": [{"id": x["id"], "tool": x["tool"], "started": x["started"],
                      "ends": x["ends"], "dev": x.get("dev")} for x in s[:20]],
    }


def result(cfg: Config, sid: str) -> dict:
    meta_path = _meta_path(cfg, sid)
    if not util.path_exists(meta_path):
        return {"id": sid, "tool": "", "lines": [], "summary": "no such session",
                "started": 0, "ended": 0}
    with open(meta_path) as fh:
        m = json.load(fh)
    text = util.read_text(_out_path(cfg, sid))
    lines = text.splitlines()[-500:]
    running = time.time() < m.get("ends", 0)
    summary = "running…" if running else f"{len(text.splitlines())} lines captured"
    return {"id": sid, "tool": m["tool"], "dev": m.get("dev"), "lines": lines,
            "summary": summary, "started": m["started"],
            "ended": 0 if running else m["ends"], "wakes_disk": m.get("wakes_disk", False)}
