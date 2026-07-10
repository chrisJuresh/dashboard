"""
a3watch.attribute — turn raw signals into ranked causes with confidence.

Honesty is the point. Block-layer signals (diskstats, cgroup io.stat) prove
*which device* and *which cgroup* did physical I/O, but several real causes
leave weak or no block-layer evidence, and we say so rather than inventing a
culprit:

  * SMART / ATA-passthrough polls (e.g. the `scrutiny` container) can spin a
    disk up WITHOUT appearing in block I/O counters → we flag the possibility
    and drop to low confidence when there is no block evidence.
  * mergerfs hides which branch served a read at the *file* level; block-level
    device attribution is still solid, so we keep device/container confidence
    but note file-path ambiguity.
  * Deferred writeback / journal commits are done by kernel threads long after
    the original writer → low confidence, labelled as writeback.
  * What blocks *package* C-states is many small wakeups from many sources;
    inherently fuzzy, so those events cap at medium confidence.
"""

from __future__ import annotations

from typing import Optional

# units known to fan out across many/all disks by design
_ALLDISK_UNITS = ("a3-maintenance", "snapraid", "fstrim", "e2scrub_all", "xfs_scrub")
# containers known to poll SMART (spin-ups may be invisible to block counters)
_SMART_POLLERS = ("scrutiny", "smartd", "scrutiny-collector")


def _is_kernel(comm: str) -> bool:
    return comm.startswith(
        ("kworker", "flush-", "jbd2", "md", "kswapd", "ksoftirqd", "xfsaild", "dmcrypt")
    )


def _unit_touches(unit: str, role: str) -> bool:
    u = unit.lower()
    if any(k in u for k in _ALLDISK_UNITS):
        return True
    return False


def detect_disk_events(window: dict) -> list[dict]:
    """Emit a spin-up event for each monitored rotational disk that went from
    standby/sleeping to active/producing I/O this cycle."""
    events: list[dict] = []
    disk_map = window["disk_map"]           # dev -> DiskCfg
    delta = window["disk_delta"]            # dev -> counters
    cur_state = window["disk_state"]
    prev_state = window["prev_disk_state"]

    for dev, dc in disk_map.items():
        if not dc.rotational or not dc.monitored:
            continue
        d = delta.get(dev, {})
        had_io = (d.get("reads", 0) > 0) or (d.get("writes", 0) > 0)
        was_asleep = prev_state.get(dev) in ("standby", "sleeping")
        now_awake = cur_state.get(dev) in ("active", "idle")
        if not (was_asleep and (had_io or now_awake)):
            continue
        ev = _build_spinup(window, dev, dc, d)
        events.append(ev)
    return events


def _build_spinup(window: dict, dev: str, dc, d: dict) -> dict:
    maj_min = dc.maj_min or window.get("maj_min", {}).get(dev, "")
    evidence: list[dict] = []
    reads, writes = d.get("reads", 0), d.get("writes", 0)
    rsect, wsect = d.get("rsect", 0), d.get("wsect", 0)
    if reads or writes:
        evidence.append(
            {
                "signal": "diskstats",
                "detail": f"{reads} reads ({rsect*512} B), {writes} writes ({wsect*512} B) this cycle",
                "weight": 1.0,
            }
        )
    # extra per-wake context
    io_ms = d.get("io_ms", 0)
    if io_ms:
        evidence.append({"signal": "io_latency",
                         "detail": f"device had I/O in flight {io_ms} ms this cycle", "weight": 0.3})
    wb = window.get("writeback_kb", 0)
    dirty = window.get("dirty_kb", 0)
    if writes and (wb or dirty > 4096):
        evidence.append({"signal": "writeback",
                         "detail": f"kernel dirty={dirty // 1024} MB, writeback={wb // 1024} MB — "
                         "this write may be a deferred page-cache flush (origin wrote earlier)",
                         "weight": 0.4})
    psi = window.get("psi_io", "")
    some = next((ln for ln in psi.splitlines() if ln.startswith("some")), "")
    if some:
        evidence.append({"signal": "io_pressure", "detail": f"PSI io {some}", "weight": 0.2})
    sd = window.get("spindown", {}).get(dev)
    if sd is None:
        evidence.append({"signal": "spindown",
                         "detail": f"{dev} has no spindown timer configured (stays awake once woken)",
                         "weight": 0.2})
    elif sd == 0:
        evidence.append({"signal": "spindown", "detail": f"{dev} spindown is disabled", "weight": 0.2})
    else:
        evidence.append({"signal": "spindown",
                         "detail": f"{dev} spindown timer is {sd:.0f} min", "weight": 0.2})

    # --- per-device container attribution (block-level, strong) ---
    cg_hits = [c for c in window["cgroup_io"] if c["dev"] == maj_min]
    cg_hits.sort(key=lambda c: c["rbytes_d"] + c["wbytes_d"], reverse=True)
    for c in cg_hits:
        evidence.append(
            {
                "signal": "cgroup_io",
                "detail": f"{c['name']}: {c['rbytes_d']} B read, {c['wbytes_d']} B write to {dev}",
                "weight": 0.9,
            }
        )

    # --- scheduled units that fan out to all disks ---
    unit_hits = [u for u in window["unit_fires"] if _unit_touches(u["unit"], dc.role)]
    for u in unit_hits:
        evidence.append(
            {"signal": "unit_fire", "detail": f"{u['unit']} ran near this time", "weight": 0.8}
        )

    # --- process-level physical I/O (device-agnostic) ---
    io_procs = [
        p
        for p in window["proc_io"]
        if (p["read_bytes_d"] + p["write_bytes_d"]) > 0
    ]
    io_procs.sort(key=lambda p: p["read_bytes_d"] + p["write_bytes_d"], reverse=True)
    nonkernel = [p for p in io_procs if not _is_kernel(p["comm"])]
    kernel = [p for p in io_procs if _is_kernel(p["comm"])]
    for p in nonkernel[:3]:
        evidence.append(
            {
                "signal": "proc_io",
                "detail": f"{p['comm']} (pid {p['pid']}): "
                f"{p['read_bytes_d']} B read, {p['write_bytes_d']} B write (device-agnostic)",
                "weight": 0.5,
            }
        )

    smart_pollers_present = any(
        any(sp in c["name"] for sp in _SMART_POLLERS) for c in window["cgroup_io"]
    ) or window.get("smart_pollers_running")

    # --- scoring ---
    confidence, cause, cause_kind, note = _score_spinup(
        dev, dc, cg_hits, unit_hits, nonkernel, kernel, smart_pollers_present, reads, writes
    )

    if dc.pool:
        note = (note + " " if note else "") + (
            f"{dev} is a mergerfs '{dc.pool}' branch; block attribution is exact "
            "but which *file* was served can't be pinned without diagnostic tracing."
        )
        evidence.append(
            {"signal": "topology", "detail": f"mergerfs pool branch: {dc.pool}", "weight": 0.2}
        )

    return {
        "dev": dev,
        "kind": "spinup",
        "confidence": confidence,
        "primary_cause": cause,
        "cause_kind": cause_kind,
        "note": note,
        "evidence": evidence,
    }


def _score_spinup(
    dev, dc, cg_hits, unit_hits, nonkernel, kernel, smart_pollers, reads, writes
) -> tuple[str, str, str, str]:
    # 1) exactly one container physically touched this device
    if len(cg_hits) == 1:
        return ("high", cg_hits[0]["name"], "container", "")
    if len(cg_hits) > 1:
        return (
            "medium",
            cg_hits[0]["name"],
            "container",
            f"{len(cg_hits)} containers did I/O to {dev}; top by bytes shown.",
        )
    # 2) a known all-disk scheduled job fired
    if unit_hits:
        multi = "; multiple disks likely woke together" if dc.role in ("parity", "data", "pool") else ""
        return ("high", unit_hits[0]["unit"], "unit", f"scheduled maintenance job{multi}.")
    # 3) a single non-kernel process did physical I/O (device link inferred)
    if len(nonkernel) == 1:
        return (
            "medium",
            nonkernel[0]["comm"],
            "process",
            "single process did physical I/O this cycle; device link inferred, not proven.",
        )
    if len(nonkernel) > 1:
        return (
            "low",
            nonkernel[0]["comm"],
            "process",
            f"{len(nonkernel)} processes did physical I/O; cannot pin which hit {dev}.",
        )
    # 4) only kernel threads → writeback / journal
    if kernel:
        return (
            "low",
            f"kernel writeback ({kernel[0]['comm']})",
            "kernel",
            "deferred writeback or journal commit; the original writer ran earlier "
            "and may be unknowable at this sampling resolution.",
        )
    # 5) no block evidence at all — SMART poll or firmware
    if smart_pollers:
        return (
            "low",
            "possible SMART poll (e.g. scrutiny)",
            "unknown",
            "disk woke with no block-I/O evidence; a SMART/ATA-passthrough poll "
            "(scrutiny/smartd) can spin a disk up invisibly to block counters. "
            "Confirm in diagnostic mode.",
        )
    return (
        "low",
        "unattributed",
        "unknown",
        "disk woke with no attributable block I/O; could be controller/firmware, "
        "an ATA passthrough, or activity between samples. Use diagnostic mode to trace.",
    )


def attribute_stay_awake(window: dict, dev: str, dc, awake_minutes: float, timer_min: float) -> dict:
    """Only called for a disk that HAS a spindown timer but has stayed awake well
    past it — a genuine 'its idle timer keeps getting reset' anomaly."""
    maj_min = dc.maj_min or ""
    cg_hits = [c for c in window["cgroup_io"] if c["dev"] == maj_min]
    evidence = [
        {
            "signal": "state",
            "detail": f"{dev} has a {timer_min:.0f}-min spindown timer but has been awake "
            f"~{awake_minutes:.0f} min without sleeping — its idle timer is being reset",
            "weight": 1.0,
        }
    ]
    if cg_hits:
        cg_hits.sort(key=lambda c: c["rbytes_d"] + c["wbytes_d"], reverse=True)
        top = cg_hits[0]
        evidence.append(
            {"signal": "cgroup_io", "detail": f"recent block I/O to {dev} from {top['name']}",
             "weight": 0.8}
        )
        return {
            "dev": dev, "kind": "stay_awake", "confidence": "medium",
            "primary_cause": top["name"], "cause_kind": "container",
            "note": f"recurring I/O from {top['name']} keeps resetting {dev}'s "
            f"{timer_min:.0f}-min spindown timer.",
            "evidence": evidence,
        }
    return {
        "dev": dev, "kind": "stay_awake", "confidence": "low",
        "primary_cause": "idle timer reset by metadata / open handles (no block I/O seen)",
        "cause_kind": "unknown",
        "note": f"{dev} should sleep after {timer_min:.0f} min but hasn't; no block I/O is "
        "visible, so it's likely metadata access or an open file handle resetting the timer. "
        "Use the wake-audit diagnostic to catch the exact accessor.",
        "evidence": evidence,
    }


# ------------------------------------------------------- power events -------
def attribute_power(window: dict, prev_pkg_w: Optional[float]) -> list[dict]:
    events: list[dict] = []
    cpu = window["cpu"]
    pkg_w = cpu.get("pkg_w")
    busy = cpu.get("busy_pct", 0.0)
    top = sorted(window.get("cpu_top", []), key=lambda t: t.get("cpu_pct", 0), reverse=True)

    detail_ctx = _top_detail(top)
    ctx = f" — {detail_ctx}" if detail_ctx else ""

    # wattage rise
    if pkg_w is not None and prev_pkg_w is not None and (pkg_w - prev_pkg_w) >= 1.0:
        cause, conf = _top_cause(top, busy)
        events.append(
            {
                "kind": "watt_rise",
                "confidence": conf,
                "primary_cause": cause,
                "detail": f"package power {prev_pkg_w:.1f}W → {pkg_w:.1f}W; "
                f"CPU busy {busy:.0f}%.{ctx}",
            }
        )

    # deep package C-state stall — decoded, and honest about cores vs package
    info = cpu.get("cstate_info", {}) or {}
    deep_avail = info.get("deep_available", True)
    deepest = info.get("deepest", "") or "?"
    pkg_cs = {name: pct for name, pct in cpu.get("pkg_cstates", [])}
    deep = pkg_cs.get("PC6", 0) + pkg_cs.get("PC8", 0) + pkg_cs.get("PC10", 0)
    shallow = pkg_cs.get("PC2", 0) + pkg_cs.get("PC3", 0)
    if pkg_cs and deep < 1.0:
        if not deep_avail:
            # genuinely no deep CORE C-states exposed -> package cannot go deep.
            # A real platform fact (rare); still not a process.
            events.append(
                {
                    "kind": "pkg_cstate_stall",
                    "confidence": "high",
                    "primary_cause": f"cores don't reach C6+ (deepest {deepest})",
                    "detail": f"the cores themselves never enter C6+, so deep package idle "
                    f"(PC6/8/10) is impossible regardless of load — a BIOS/kernel C-state "
                    f"matter, not a process. CPU is {busy:.0f}% busy.",
                }
            )
        elif busy < 25.0:
            # cores DO reach deep C-states but the PACKAGE won't. NOT "no deep
            # states", NOT a busy process to kill. Report the true divide and give
            # candidate causes with explicit uncertainty (low confidence).
            events.append(
                {
                    "kind": "pkg_cstate_stall",
                    "confidence": "low",
                    "primary_cause": "package held at PC3 (uncore/iGPU/PCIe or BIOS package-C-state limit)",
                    "detail": f"cores reach deep C-states ({deepest}) and the box is only "
                    f"{busy:.0f}% busy, yet package PC6/8/10 stay ~0% (PC2+PC3 ≈ {shallow:.0f}%). "
                    f"The package — not the cores — is being kept out of deep idle. Likely "
                    f"candidates (uncertain, not proven): a BIOS 'Package C-State Limit' set "
                    f"shallower than the per-core setting, an always-active PCIe/uncore agent, "
                    f"or the iGPU not holding RC6. A platform matter to confirm in BIOS or with "
                    f"a diagnostic turbostat/powertop run — not a process a3watch can attribute.",
                }
            )
    return events


def _describe(t: dict) -> str:
    """A PID rendered as something meaningful: comm (pid) [in container/service]."""
    who = f"{t.get('comm', '?')} (pid {t.get('pid', '?')})"
    cg = t.get("cgroup_name") or ""
    if cg and cg not in ("root", "", "/"):
        who += f" in {cg}"
    return who


def _top_detail(top: list[dict]) -> str:
    """One-line context for the leading non-kernel CPU user: what it actually is."""
    nonkernel = [t for t in top if not _is_kernel(t.get("comm", ""))]
    if not nonkernel:
        return ""
    lead = nonkernel[0]
    parts = [f"top: {lead.get('comm', '?')} (pid {lead.get('pid', '?')}, "
             f"{lead.get('cpu_pct', 0.0):.0f}% CPU)"]
    cg = lead.get("cgroup_name") or ""
    if cg and cg not in ("root", "", "/"):
        parts.append(f"in {cg}")
    cmd = lead.get("cmdline") or ""
    if cmd:
        parts.append(f"cmd: {cmd}")
    return " · ".join(parts)


def _top_cause(top: list[dict], busy: float) -> tuple[str, str]:
    nonkernel = [t for t in top if not _is_kernel(t.get("comm", ""))]
    if not nonkernel:
        return ("kernel / IRQ activity", "low")
    lead = nonkernel[0]
    share = lead.get("cpu_pct", 0.0)
    if len(nonkernel) == 1 or share >= max(1.0, 0.6 * busy * 1.0):
        return (_describe(lead), "medium")
    names = ", ".join(f"{t['comm']}" for t in nonkernel[:3])
    return (names, "low")
