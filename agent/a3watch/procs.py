"""
a3watch.procs — process & container attribution inputs.

  * /proc/<pid>/io read_bytes/write_bytes = bytes that actually crossed the
    block layer (NOT satisfied from page cache) → the strongest cheap signal
    that a process caused physical disk I/O.
  * /proc/<pid>/stat utime+stime → CPU attribution for wattage / C-state stalls.
  * cgroup v2 io.stat → per-container physical bytes per device, so a spin-up
    can be pinned to jellyfin / scrutiny / etc. even when the exact PID is gone.

All reads are of /proc and /sys pseudo-files. `docker ps` is used only to map
container ids to friendly names and read restart state; it talks to the docker
socket, not a disk.
"""

from __future__ import annotations

import os

from . import util

_CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
_CGROUP_ROOT = "/sys/fs/cgroup"


# ------------------------------------------------------- /proc readers ------
def _parse_stat(pid: int) -> dict | None:
    raw = util.read_text(f"/proc/{pid}/stat")
    if not raw:
        return None
    # comm is in parens and may contain spaces/parens: split on the last ')'
    rp = raw.rfind(")")
    if rp < 0:
        return None
    comm = raw[raw.find("(") + 1 : rp]
    rest = raw[rp + 2 :].split()
    try:
        # rest[0]=state, rest[1]=ppid ... utime=rest[11], stime=rest[12],
        # starttime=rest[19] (0-based into the post-comm fields)
        state = rest[0]
        ppid = int(rest[1])
        utime = int(rest[11])
        stime = int(rest[12])
        starttime = int(rest[19])
    except (IndexError, ValueError):
        return None
    return {
        "comm": comm,
        "state": state,
        "ppid": ppid,
        "ticks": utime + stime,
        "starttime": starttime,
    }


def _read_io(pid: int) -> tuple[int, int]:
    read_bytes = write_bytes = 0
    for line in util.read_text(f"/proc/{pid}/io").splitlines():
        # a torn read of an exiting process can yield a partial line; skip it
        # rather than let one bad value blank the whole cycle's attribution.
        try:
            if line.startswith("read_bytes:"):
                read_bytes = int(line.split(":")[1])
            elif line.startswith("write_bytes:"):
                write_bytes = int(line.split(":")[1])
        except (ValueError, IndexError):
            continue
    return (read_bytes, write_bytes)


def _read_cgroup_path(pid: int) -> str:
    for line in util.read_text(f"/proc/{pid}/cgroup").splitlines():
        # cgroup v2: "0::/system.slice/docker-<id>.scope"
        if line.startswith("0::"):
            return line[3:]
    return ""


def read_proc_io() -> dict[int, dict]:
    """{pid: {comm,state,ppid,ticks,starttime,read_bytes,write_bytes,cgroup}}."""
    out: dict[int, dict] = {}
    for name in util.list_dir("/proc"):
        if not name.isdigit():
            continue
        pid = int(name)
        st = _parse_stat(pid)
        if st is None:
            continue
        rb, wb = _read_io(pid)
        st["read_bytes"] = rb
        st["write_bytes"] = wb
        st["cgroup"] = _read_cgroup_path(pid)
        out[pid] = st
    return out


def proc_io_delta(prev: dict[int, dict], cur: dict[int, dict], dt: float) -> list[dict]:
    """Per-pid deltas for pids present in both cycles with the same starttime
    (guards against PID reuse). cpu_ms_d from utime+stime ticks."""
    out: list[dict] = []
    if dt <= 0:
        return out
    for pid, c in cur.items():
        p = prev.get(pid)
        if not p or p.get("starttime") != c.get("starttime"):
            continue
        rbd = c["read_bytes"] - p["read_bytes"]
        wbd = c["write_bytes"] - p["write_bytes"]
        cpu_ms = (c["ticks"] - p["ticks"]) * 1000.0 / _CLK_TCK
        if rbd < 0:
            rbd = 0
        if wbd < 0:
            wbd = 0
        if cpu_ms < 0:
            cpu_ms = 0.0
        out.append(
            {
                "pid": pid,
                "comm": c["comm"],
                "cgroup": c["cgroup"],
                "read_bytes_d": rbd,
                "write_bytes_d": wbd,
                "cpu_ms_d": cpu_ms,
                "cpu_pct": cpu_ms / (dt * 1000.0) * 100.0,
                "state": c["state"],
                "ppid": c["ppid"],
            }
        )
    return out


# ----------------------------------------------------------- cgroup io ------
def read_cgroup_io(cgroup_paths: set[str]) -> dict[str, dict[str, tuple[int, int]]]:
    """For each cgroup path read io.stat → {path: {'maj:min': (rbytes,wbytes)}}."""
    out: dict[str, dict[str, tuple[int, int]]] = {}
    for path in cgroup_paths:
        if not path or path == "/":
            continue
        fpath = os.path.join(_CGROUP_ROOT, path.lstrip("/"), "io.stat")
        txt = util.read_text(fpath)
        if not txt:
            continue
        devs: dict[str, tuple[int, int]] = {}
        for line in txt.splitlines():
            parts = line.split()
            if not parts:
                continue
            dev = parts[0]
            rb = wb = 0
            for kv in parts[1:]:
                try:
                    if kv.startswith("rbytes="):
                        rb = int(kv.split("=")[1])
                    elif kv.startswith("wbytes="):
                        wb = int(kv.split("=")[1])
                except (ValueError, IndexError):
                    continue
            devs[dev] = (rb, wb)
        if devs:
            out[path] = devs
    return out


def cgroup_io_delta(
    prev: dict[str, dict[str, tuple[int, int]]],
    cur: dict[str, dict[str, tuple[int, int]]],
    name_map: dict[str, str] | None = None,
) -> list[dict]:
    out: list[dict] = []
    for path, devs in cur.items():
        p = prev.get(path, {})
        for dev, (rb, wb) in devs.items():
            prb, pwb = p.get(dev, (0, 0))
            rbd, wbd = rb - prb, wb - pwb
            if rbd < 0:
                rbd = 0
            if wbd < 0:
                wbd = 0
            if rbd == 0 and wbd == 0:
                continue
            out.append(
                {
                    "name": friendly_cgroup_name(path, name_map),
                    "path": path,
                    "dev": dev,
                    "rbytes_d": rbd,
                    "wbytes_d": wbd,
                }
            )
    return out


def friendly_cgroup_name(path: str, name_map: dict[str, str] | None = None) -> str:
    tail = path.rstrip("/").split("/")[-1] if path else ""
    if "docker-" in tail and tail.endswith(".scope"):
        cid = tail[len("docker-") : -len(".scope")]
        if name_map and cid[:12] in name_map:
            return name_map[cid[:12]]
        if name_map and cid in name_map:
            return name_map[cid]
        return f"docker:{cid[:12]}"
    if tail.endswith(".service"):
        return tail[: -len(".service")]
    if tail.endswith(".scope"):
        return tail[: -len(".scope")]
    return tail or path or "root"


def refresh_container_names() -> dict[str, str]:
    """Best-effort {container_id[:12]: name} via docker ps. Never fatal."""
    rc, out, _ = util.run_cmd(
        ["docker", "ps", "--no-trunc", "--format", "{{.ID}}\t{{.Names}}"],
        timeout=5.0,
        allow_diagnostic=True,  # docker socket, not a disk; benign
    )
    names: dict[str, str] = {}
    if rc == 0:
        for line in out.splitlines():
            if "\t" in line:
                cid, name = line.split("\t", 1)
                names[cid[:12]] = name.strip()
                names[cid] = name.strip()
    return names


def container_states() -> list[dict]:
    """[{name,status,restarts,running}] via docker ps -a. Best-effort."""
    rc, out, _ = util.run_cmd(
        ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Status}}"],
        timeout=5.0,
        allow_diagnostic=True,
    )
    res: list[dict] = []
    if rc != 0:
        return res
    for line in out.splitlines():
        if "\t" not in line:
            continue
        name, status = line.split("\t", 1)
        res.append(
            {
                "name": name.strip(),
                "status": status.strip(),
                "running": status.startswith("Up"),
                "restarting": "Restarting" in status,
            }
        )
    return res


# ---------------------------------------------------------- stray flags -----
_KERNEL_HINTS = ("kworker", "flush-", "jbd2", "md", "kswapd", "ksoftirqd", "migration")


def is_kernel_thread(proc: dict) -> bool:
    return proc.get("ppid") == 2 or proc.get("comm", "").startswith(_KERNEL_HINTS)


def flag_stray(procs_delta: list[dict], containers: list[dict], cpu_pct_threshold: float = 5.0) -> list[dict]:
    """Detect crash-looping containers, stray/orphan processes and heavy
    unmanaged CPU users. Conservative: only clear signals are flagged."""
    flags: list[dict] = []
    for c in containers:
        if c.get("restarting"):
            flags.append(
                {
                    "pid": 0,
                    "comm": c["name"],
                    "cgroup": "docker",
                    "flag": "crashloop",
                    "note": f"container status: {c['status']}",
                }
            )
    for p in procs_delta:
        cg = p.get("cgroup", "")
        cpu = p.get("cpu_pct", 0.0)
        if is_kernel_thread(p):
            continue
        managed = (
            cg.startswith("/system.slice")
            or cg.startswith("/user.slice")
            or "docker-" in cg
            or cg.startswith("/machine.slice")
        )
        if not managed and cpu >= cpu_pct_threshold and p.get("ppid") == 1:
            flags.append(
                {
                    "pid": p["pid"],
                    "comm": p["comm"],
                    "cgroup": cg or "(none)",
                    "flag": "stray",
                    "note": f"{cpu:.0f}% CPU, orphaned (ppid=1), not in a service cgroup",
                }
            )
    return flags
