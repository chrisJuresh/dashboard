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


def _diag_dir(cfg: Config) -> str:
    os.makedirs(cfg.diag_dir, exist_ok=True)
    return cfg.diag_dir


def _meta_path(cfg: Config, sid: str) -> str:
    return os.path.join(cfg.diag_dir, f"{sid}.json")


def _out_path(cfg: Config, sid: str) -> str:
    return os.path.join(cfg.diag_dir, f"{sid}.out")


def start(cfg: Config, tool: str, seconds: int, dev: str | None, confirm_wake: bool) -> str:
    _diag_dir(cfg)
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
