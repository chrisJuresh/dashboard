"""
Per-container health for Docker: state, restart episodes, and (for running
containers) CPU% + memory from cgroup v2. Non-waking by construction:

  * The only subprocess is `docker ps` (allowed, non-waking) via safe.run.
  * CPU/mem come from cgroup v2 pseudo-files under /sys/fs/cgroup, which are
    served from memory and never touch a disk platter.

The single most valuable signal here is the crash-loop flag: a container stuck
in "Restarting" (e.g. a mis-configured jellyfin) shows up as .state="restarting"
and .restarting=1, and each fresh restart episode is counted in .restarts.

cgroup id <-> name mapping is normally awkward, so we append `{{.ID}}` to the
requested `docker ps` format (a single extra column, one subprocess) and match
each container's short id against the docker-<id>.scope directory name. That
lets CPU% and memory be keyed by the human container NAME. If the scope can't
be found (stopped container, cgroupfs driver, permissions) we simply skip
cpu/mem for that container — every read tolerates missing files.
"""
from __future__ import annotations

import os

from . import Collector, Metric, Ctx, register, safe

_MB = 1024 * 1024
_CGROUP_BASES = (
    "/sys/fs/cgroup/system.slice",  # systemd cgroup driver (docker-<id>.scope)
    "/sys/fs/cgroup/docker",        # cgroupfs driver (<id>/)
)


def _state_from_status(status: str) -> str:
    """Normalise a `docker ps` Status string to a docker container state."""
    low = status.strip().lower()
    if not low:
        return "unknown"
    if low.startswith("up"):
        return "paused" if "(paused)" in low else "running"
    for prefix in ("restarting", "exited", "created", "dead", "removal", "removing"):
        if low.startswith(prefix):
            return "removing" if prefix in ("removal", "removing") else prefix
    return low.split()[0]


def _scope_dirs() -> dict:
    """Map full container id -> cgroup path for every live docker scope found."""
    out: dict = {}
    for base in _CGROUP_BASES:
        try:
            entries = os.listdir(base)
        except OSError:
            continue
        for d in entries:
            if d.startswith("docker-") and d.endswith(".scope"):
                cid = d[len("docker-"):-len(".scope")]
            elif base.endswith("/docker") and len(d) >= 12 and all(c in "0123456789abcdef" for c in d):
                cid = d
            else:
                continue
            out[cid] = os.path.join(base, d)
    return out


def _find_scope(scopes: dict, short_id: str):
    if not short_id:
        return None
    exact = scopes.get(short_id)
    if exact:
        return exact
    for cid, path in scopes.items():
        if cid.startswith(short_id):
            return path
    return None


def _cgroup_cpu_usec(scope: str):
    for line in safe.read_text(os.path.join(scope, "cpu.stat")).splitlines():
        p = line.split()
        if len(p) == 2 and p[0] == "usage_usec":
            try:
                return int(p[1])
            except ValueError:
                return None
    return None


class Containers(Collector):
    name = "containers"
    group = "Containers"
    every_cycles = 3  # docker ps + cgroup walk — run ~every minute, not every cycle

    def collect(self, ctx: Ctx) -> list[Metric]:
        rc, out, _ = safe.run(
            ["docker", "ps", "-a", "--format",
             "{{.Names}}\t{{.Status}}\t{{.RunningFor}}\t{{.ID}}"],
            timeout=6.0,
        )
        if rc != 0 or not out.strip():
            return []

        scopes = _scope_dirs()
        prev_all = ctx.prev.get(self.name, {})   # {name: {state,restarts,cpu_usec,ts}}
        new_all: dict = {}
        metrics: list[Metric] = []
        n_running = n_restarting = n_total = 0

        for line in out.splitlines():
            fields = line.split("\t")
            if not fields or not fields[0].strip():
                continue
            name = fields[0].strip()
            status = fields[1].strip() if len(fields) > 1 else ""
            short_id = fields[3].strip() if len(fields) > 3 else ""

            state = _state_from_status(status)
            restarting = 1 if state == "restarting" else 0
            n_total += 1
            if state == "running":
                n_running += 1
            if restarting:
                n_restarting += 1

            prev_c = prev_all.get(name, {})
            restarts = int(prev_c.get("restarts", 0) or 0)
            prev_state = prev_c.get("state")
            # count each fresh entry into the restarting state as one episode
            if prev_state is not None and prev_state != "restarting" and state == "restarting":
                restarts += 1

            new_c: dict = {"state": state, "restarts": restarts}

            metrics.append(Metric(f"{name}.state", text=state))
            metrics.append(Metric(f"{name}.status", text=status or "?"))
            metrics.append(Metric(f"{name}.restarting", num=restarting,
                                  text="restarting" if restarting else "stable"))
            metrics.append(Metric(f"{name}.restarts", num=restarts,
                                  unit="episodes"))

            # CPU% + memory for running containers with a resolvable cgroup scope
            scope = _find_scope(scopes, short_id)
            if scope:
                mem = safe.read_int(os.path.join(scope, "memory.current"))
                if mem is not None:
                    metrics.append(Metric(f"{name}.mem",
                                          num=round(mem / _MB, 1), unit="MB",
                                          series=True))
                cur_usec = _cgroup_cpu_usec(scope)
                if cur_usec is not None:
                    prev_usec = prev_c.get("cpu_usec")
                    prev_ts = prev_c.get("ts")
                    if prev_usec is not None and prev_ts:
                        dt = ctx.ts - prev_ts
                        d_usec = cur_usec - prev_usec
                        # guard against reboot / counter reset (negative delta)
                        if dt > 0 and d_usec >= 0:
                            cpu_pct = d_usec / 1e6 / dt * 100.0
                            metrics.append(Metric(f"{name}.cpu_pct",
                                                  num=round(cpu_pct, 1), unit="%",
                                                  series=True))
                    new_c["cpu_usec"] = cur_usec
                    new_c["ts"] = ctx.ts

            new_all[name] = new_c

        ctx.prev[self.name] = new_all

        metrics.append(Metric("_summary.total", num=n_total))
        metrics.append(Metric("_summary.running", num=n_running))
        metrics.append(Metric("_summary.restarting", num=n_restarting,
                              series=True))
        return metrics


register(Containers())
