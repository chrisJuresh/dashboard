"""
a3watch.sample — one sampling cycle (the systemd timer target).

Design constraints:
  * Cheap: reads pseudo-files, does small SQLite writes, exits. No busy loop.
  * Non-waking: the only disk-touching call is disks.power_state(), which uses
    hdparm -C (non-waking) and only for non-protected drives.
  * Robust: every reader is wrapped so a single failure can't abort the cycle;
    a boot-id change or an implausible dt discards deltas rather than lying.

Deltas are computed against the previous cycle's raw counters, persisted in the
`kv` table under 'last_raw', so the fresh process each tick still sees history.
"""

from __future__ import annotations

import resource
from typing import Any

from . import attribute, db, disks, power, procs, util
from .config import Config

# units that, when they run, are expected to touch one or many disks
_WATCH_UNITS = (
    "a3-maintenance.service",
    "snapraid.service",
    "snapraid-sync.service",
    "snapraid-scrub.service",
    "fstrim.service",
    "e2scrub_all.service",
    "xfs_scrub_all.service",
)
_STAY_AWAKE_MIN = 30.0          # minutes awake before we flag stay_awake
_STAY_AWAKE_THROTTLE = 3600.0   # at most one stay_awake per disk per hour
_STALL_THROTTLE = 3600.0        # at most one pkg_cstate_stall event per hour
_WATT_THROTTLE = 300.0
_OVERHEAD_EVERY = 600.0
_NAMES_TTL = 300.0
_BUDGET_ASSUMED_WATTS_PER_CORE = 3.0


def _read_unit_fires(dt: float) -> list[dict]:
    """Which watch units entered 'active' within the last cycle (+slack)."""
    boot_s = 0.0
    up = util.read_first_line("/proc/uptime")
    try:
        boot_s = float(up.split()[0])
    except (ValueError, IndexError):
        return []
    now_us = boot_s * 1_000_000.0
    window_us = (dt + 8.0) * 1_000_000.0
    fires: list[dict] = []
    for unit in _WATCH_UNITS:
        rc, out, _ = util.run_cmd(
            ["systemctl", "show", unit, "-p", "ActiveEnterTimestampMonotonic"], timeout=3.0
        )
        if rc != 0:
            continue
        val = out.strip().split("=", 1)[-1]
        try:
            ts_us = int(val)
        except ValueError:
            continue
        if ts_us > 0 and (now_us - ts_us) <= window_us:
            fires.append({"unit": unit, "exec_ts": util.now()})
    return fires


def run_once(cfg: Config) -> dict:
    m0 = util.monotonic()
    ru0 = resource.getrusage(resource.RUSAGE_SELF)
    ts = util.now()
    boot = util.boot_id()

    conn = db.connect(cfg.db_path)
    try:
        db.init_schema(conn)
        return _cycle(cfg, conn, ts, boot, m0, ru0)
    finally:
        conn.close()


def _cycle(cfg: Config, conn, ts: float, boot: str, m0: float, ru0) -> dict:
    prev: dict[str, Any] = db.get_json(conn, "last_raw", {}) or {}

    # ---- read current raw (all non-waking) ----
    cur_rapl = _safe(power.read_rapl_energy, {})
    cur_core = _safe(power.read_core_cstates, {})
    cur_pkg = _safe(power.read_pkg_cstates, {})
    cur_busy = _safe(power.read_cpu_busy, {})
    cur_ds = _safe(disks.read_diskstats, {})
    cur_procs = _safe(procs.read_proc_io, {})
    cg_paths = {p.get("cgroup", "") for p in cur_procs.values()}
    cur_cgio = _safe(lambda: procs.read_cgroup_io(cg_paths), {})

    prev_ts = prev.get("ts")
    dt = (ts - prev_ts) if prev_ts else 0.0
    reset = (
        not prev
        or prev.get("boot_id") != boot
        or dt <= 0
        or dt > 20 * max(1, cfg.interval_s)
    )

    # ---- disk power states (non-waking; hdparm -C only for non-protected) ----
    disk_state: dict[str, str] = {}
    for dc in cfg.disks:
        if not dc.monitored:
            continue
        disk_state[dc.dev] = _safe(lambda d=dc.dev: disks.power_state(cfg, d), "unknown")

    cycle = int(prev.get("cycle", 0)) + 1
    from .collect import electricity as _elec
    price_info = _safe(
        lambda: _elec.current_price(prev, ts, cfg),
        {"price": cfg.electricity_gbp_per_kwh, "source": "fallback", "updated": ts, "live": False},
    )
    prev["_cycle_next"] = cycle
    prev["_electricity_next"] = price_info

    summary = {"ts": ts, "reset": reset, "events": 0, "power_events": 0}

    if not reset:
        dt = float(dt)
        watts = power.rapl_watts(prev.get("rapl", {}), cur_rapl, dt)
        pkg_w = watts.get("package")
        core_w = watts.get("core")
        busy_pct, iowait, irq = power.cpu_busy_pct(prev.get("busy", {}), cur_busy)
        core_res = power.core_cstate_residency(prev.get("core", {}), cur_core, dt)
        pkg_res = power.pkg_cstate_residency(prev.get("pkg", {}), cur_pkg)
        ds_delta = disks.diskstats_delta(prev.get("diskstats", {}), cur_ds)
        # JSON object keys are strings; proc deltas are keyed by int pid.
        prev_procs = {int(k): v for k, v in prev.get("procs", {}).items()}
        pdelta = procs.proc_io_delta(prev_procs, cur_procs, dt)
        name_map, names_ts = _container_names(prev, ts)
        cg_delta = procs.cgroup_io_delta(prev.get("cgio", {}), cur_cgio, name_map)
        containers = _safe(procs.container_states, [])
        unit_fires = _safe(lambda: _read_unit_fires(dt), [])

        cpu_top = sorted(pdelta, key=lambda p: p["cpu_pct"], reverse=True)[:8]
        # enrich with a human-meaningful identity: container/service + command line,
        # so a PID on the dashboard says *what* it is rather than just a number
        for t in cpu_top:
            t["cgroup_name"] = procs.friendly_cgroup_name(t.get("cgroup", ""), name_map)
            t["cmdline"] = procs.read_cmdline(t["pid"])
        window = {
            "ts": ts,
            "dt": dt,
            "disk_map": {dc.dev: dc for dc in cfg.disks},
            "disk_delta": ds_delta,
            "disk_state": disk_state,
            "prev_disk_state": prev.get("disk_state", {}),
            "maj_min": {dc.dev: dc.maj_min for dc in cfg.disks},
            "proc_io": pdelta,
            "cgroup_io": cg_delta,
            "unit_fires": unit_fires,
            "smart_pollers_running": any(
                s.get("running") and any(k in s["name"] for k in ("scrutiny", "smartd"))
                for s in containers
            ),
            "cpu": {
                "pkg_w": pkg_w,
                "core_w": core_w,
                "busy_pct": busy_pct,
                "pkg_cstates": pkg_res,
            },
            "cpu_top": cpu_top,
        }

        # ---- persist time series ----
        _insert_series(conn, ts, cfg, dt, watts, busy_pct, iowait, irq,
                       core_res, pkg_res, cpu_top, ds_delta, disk_state, pdelta, cg_delta,
                       unit_fires)

        # ---- events ----
        summary["events"] = _persist_disk_events(conn, ts, window)
        summary["events"] += _persist_stay_awake(conn, ts, cfg, window, prev, disk_state)
        summary["power_events"] = _persist_power_events(conn, ts, window, prev)
        _persist_stray(conn, ts, pdelta, containers)

        # ---- overhead accounting + housekeeping ----
        _update_overhead(conn, cfg, ts, prev, pkg_w, busy_pct, cur_core.get("ncpu", 1),
                         price_info["price"])
        _rollup_and_prune(conn, cfg, ts, prev)
    else:
        name_map, names_ts = _container_names(prev, ts)

    _upsert_topology(conn, cfg, ts)

    # ---- pluggable server-info collectors (every cycle; no new wakeups) ----
    prev["_collector_state_next"] = _run_collectors(
        cfg, conn, ts, cycle, disk_state, prev.get("collector_state", {}), price_info)

    # ---- self overhead for THIS cycle ----
    ru1 = resource.getrusage(resource.RUSAGE_SELF)
    wall_ms = (util.monotonic() - m0) * 1000.0
    cpu_ms = ((ru1.ru_utime + ru1.ru_stime) - (ru0.ru_utime + ru0.ru_stime)) * 1000.0
    rss_kb = ru1.ru_maxrss
    dbb = db.db_size_bytes(cfg.db_path)
    db.insert_many(
        conn, "sample",
        ["ts", "interval_s", "boot_id", "self_wall_ms", "self_cpu_ms", "self_rss_kb", "db_bytes"],
        [(ts, cfg.interval_s, boot, wall_ms, cpu_ms, rss_kb, dbb)],
    )

    # ---- persist raw for next cycle (compact) ----
    db.set_json(conn, "last_raw", _pack_raw(cfg, ts, boot, cur_rapl, cur_core, cur_pkg,
                                            cur_busy, cur_ds, cur_procs, cur_cgio,
                                            disk_state, prev, name_map, names_ts))
    summary.update({"wall_ms": round(wall_ms, 1), "cpu_ms": round(cpu_ms, 1), "db_bytes": dbb})
    return summary


# ------------------------------------------------------------ helpers -------
def _safe(fn, default):
    try:
        return fn()
    except Exception:
        return default


def _container_names(prev: dict, ts: float) -> tuple[dict, float]:
    names = prev.get("container_names", {})
    names_ts = prev.get("container_names_ts", 0)
    if ts - names_ts > _NAMES_TTL:
        fresh = _safe(procs.refresh_container_names, {})
        if fresh:
            return fresh, ts
    return names, names_ts


def _insert_series(conn, ts, cfg, dt, watts, busy, iowait, irq, core_res, pkg_res,
                   cpu_top, ds_delta, disk_state, pdelta, cg_delta, unit_fires):
    db.insert_many(conn, "cpu_power", ["ts", "domain", "watts"],
                   [(ts, dom, w) for dom, w in watts.items()])
    db.insert_many(conn, "cpu_busy", ["ts", "busy_pct", "iowait_pct", "irq_pct"],
                   [(ts, busy, iowait, irq)])
    db.insert_many(conn, "cstate", ["ts", "scope", "name", "residency_pct"],
                   [(ts, "core", n, p) for n, p in core_res] +
                   [(ts, "package", n, p) for n, p in pkg_res])
    db.insert_many(conn, "cpu_top", ["ts", "pid", "comm", "cgroup", "cpu_pct"],
                   [(ts, t["pid"], t["comm"], t.get("cgroup", ""), t["cpu_pct"]) for t in cpu_top])
    rows = []
    for dc in cfg.disks:
        if not dc.monitored:
            continue
        d = ds_delta.get(dc.dev, {})
        active = 1 if (d.get("reads", 0) or d.get("writes", 0)) else 0
        rows.append((ts, dc.dev, d.get("reads", 0), d.get("writes", 0), d.get("rsect", 0),
                     d.get("wsect", 0), d.get("io_ms", 0), disk_state.get(dc.dev, "unknown"), active))
    db.insert_many(conn, "disk_sample",
                   ["ts", "dev", "reads_d", "writes_d", "rsect_d", "wsect_d", "io_ms_d",
                    "power_state", "active"], rows)
    top_io = sorted(pdelta, key=lambda p: p["read_bytes_d"] + p["write_bytes_d"], reverse=True)
    top_io = [p for p in top_io if p["read_bytes_d"] + p["write_bytes_d"] > 0][:15]
    db.insert_many(conn, "proc_io", ["ts", "pid", "comm", "cgroup", "read_bytes_d", "write_bytes_d"],
                   [(ts, p["pid"], p["comm"], p["cgroup"], p["read_bytes_d"], p["write_bytes_d"])
                    for p in top_io])
    db.insert_many(conn, "cgroup_io", ["ts", "name", "dev", "rbytes_d", "wbytes_d"],
                   [(ts, c["name"], c["dev"], c["rbytes_d"], c["wbytes_d"]) for c in cg_delta])
    db.insert_many(conn, "unit_fire", ["ts", "unit", "exec_ts"],
                   [(ts, u["unit"], u["exec_ts"]) for u in unit_fires])


def _persist_disk_events(conn, ts, window) -> int:
    events = attribute.detect_disk_events(window)
    for ev in events:
        _write_disk_event(conn, ts, ev)
    return len(events)


def _write_disk_event(conn, ts, ev):
    cur = conn.execute(
        "INSERT INTO disk_event(ts,dev,kind,confidence,primary_cause,cause_kind,note) "
        "VALUES(?,?,?,?,?,?,?)",
        (ts, ev["dev"], ev["kind"], ev["confidence"], ev["primary_cause"],
         ev["cause_kind"], ev["note"]),
    )
    eid = cur.lastrowid
    db.insert_many(conn, "event_evidence", ["event_id", "signal", "detail", "weight"],
                   [(eid, e["signal"], e["detail"], e["weight"]) for e in ev["evidence"]])


def _persist_stay_awake(conn, ts, cfg, window, prev, disk_state) -> int:
    awake_since = dict(prev.get("awake_since", {}))
    last_flag = dict(prev.get("stay_awake_ts", {}))
    n = 0
    for dc in cfg.disks:
        if not (dc.rotational and dc.monitored):
            continue
        st = disk_state.get(dc.dev, "unknown")
        if st in ("standby", "sleeping"):
            awake_since.pop(dc.dev, None)
            continue
        if st in ("active", "idle"):
            awake_since.setdefault(dc.dev, ts)
            mins = (ts - awake_since[dc.dev]) / 60.0
            had_spinup = any(window["disk_delta"].get(dc.dev, {}).get(k, 0)
                             for k in ("reads", "writes"))
            if (mins >= _STAY_AWAKE_MIN and not had_spinup
                    and ts - last_flag.get(dc.dev, 0) > _STAY_AWAKE_THROTTLE):
                ev = attribute.attribute_stay_awake(window, dc.dev, dc, mins)
                _write_disk_event(conn, ts, ev)
                last_flag[dc.dev] = ts
                n += 1
    # stash back
    prev["_awake_since_next"] = awake_since
    prev["_stay_awake_ts_next"] = last_flag
    return n


def _persist_power_events(conn, ts, window, prev) -> int:
    last = dict(prev.get("power_event_ts", {}))
    evs = attribute.attribute_power(window, prev.get("prev_pkg_w"))
    n = 0
    for ev in evs:
        throttle = _STALL_THROTTLE if ev["kind"] == "pkg_cstate_stall" else _WATT_THROTTLE
        if ts - last.get(ev["kind"], 0) < throttle:
            continue
        conn.execute(
            "INSERT INTO power_event(ts,kind,confidence,primary_cause,detail) VALUES(?,?,?,?,?)",
            (ts, ev["kind"], ev["confidence"], ev["primary_cause"], ev["detail"]),
        )
        last[ev["kind"]] = ts
        n += 1
    prev["_power_event_ts_next"] = last
    prev["_prev_pkg_w_next"] = window["cpu"].get("pkg_w")
    return n


def _persist_stray(conn, ts, pdelta, containers):
    flags = procs.flag_stray(pdelta, containers)
    db.insert_many(conn, "proc_flag", ["ts", "pid", "comm", "cgroup", "flag", "note"],
                   [(ts, f["pid"], f["comm"], f["cgroup"], f["flag"], f["note"]) for f in flags])


def _run_collectors(cfg, conn, ts, cycle, disk_state, coll_prev, price_info):
    """Run every enabled, due collector once (in this same cycle — no new timers),
    store their latest snapshot + series history. A collector can only touch a disk
    via collect.safe's gated helpers, so this never wakes/edits an HDD."""
    from . import collect
    reg = collect.load_collectors()
    awake = {dev for dev, st in disk_state.items() if st in ("idle", "active")}
    ctx = collect.Ctx(ts=ts, interval_s=cfg.interval_s, cycle=cycle, awake_disks=awake,
                      disks=cfg.disks, price_gbp_kwh=price_info.get("price", 0.0),
                      budget_gbp_year=cfg.budget_gbp_year,
                      prev=dict(coll_prev or {}))
    ctx.prev["electricity"] = price_info  # let the electricity collector read source/live
    latest, series = [], []
    for name, c in reg.items():
        if name in cfg.disabled_collectors:
            continue
        if c.every_cycles > 1 and (cycle % c.every_cycles) != 0:
            continue
        try:
            metrics = c.collect(ctx) or []
        except Exception:
            continue
        for m in metrics:
            latest.append((name, c.group, m.key, m.num, m.text, m.unit, ts))
            if m.series and m.num is not None:
                series.append((ts, f"{name}.{m.key}", m.num))
    db.insert_many(conn, "metric_latest",
                   ["collector", "grp", "key", "num", "txt", "unit", "ts"], latest)
    db.insert_many(conn, "metric_series", ["ts", "key", "num"], series)
    return ctx.prev


def _update_overhead(conn, cfg, ts, prev, pkg_w, busy_pct, ncpu, price):
    last_ov = prev.get("overhead_ts", 0)
    # EMA of the incremental power of one active core, learned from RAPL when busy
    acw = prev.get("active_core_watts_ema", _BUDGET_ASSUMED_WATTS_PER_CORE)
    if pkg_w and busy_pct and busy_pct > 3.0 and ncpu:
        inst = pkg_w / max(1.0, (busy_pct / 100.0) * ncpu)
        inst = min(6.0, max(1.0, inst))
        acw = 0.9 * acw + 0.1 * inst
    prev["_active_core_watts_next"] = acw
    if ts - last_ov < _OVERHEAD_EVERY:
        return
    # derive cpu-seconds/day from the sample table (mean self_cpu_ms over recent rows)
    row = conn.execute(
        "SELECT AVG(self_cpu_ms) a, COUNT(*) c FROM sample WHERE ts > ?", (ts - 86400,)
    ).fetchone()
    mean_cpu_ms = (row["a"] or 0.0)
    cycles_day = 86400.0 / max(1, cfg.interval_s)
    cpu_ms_day = mean_cpu_ms * cycles_day
    cpu_s_day = cpu_ms_day / 1000.0
    avg_watts = (cpu_s_day / 86400.0) * acw
    gbp_year = avg_watts / 1000.0 * 8760.0 * price
    dbb = db.db_size_bytes(cfg.db_path)
    samples = conn.execute("SELECT COUNT(*) c FROM sample").fetchone()["c"]
    conn.execute(
        "INSERT OR REPLACE INTO overhead(ts,cpu_ms_day,avg_watts,gbp_year,db_bytes,samples) "
        "VALUES(?,?,?,?,?,?)",
        (ts, cpu_ms_day, avg_watts, gbp_year, dbb, samples),
    )
    prev["_overhead_ts_next"] = ts


def _rollup_and_prune(conn, cfg, ts, prev):
    last_hour = prev.get("rollup_hour", 0)
    cur_hour = int(ts // 3600)
    # roll up any fully-completed hours (cap at 6 to bound work)
    start = last_hour if last_hour else cur_hour - 1
    done = last_hour
    for h in range(max(start, cur_hour - 6), cur_hour):
        if h <= last_hour:
            continue
        db.rollup_hour(conn, h)
        done = h
    prev["_rollup_hour_next"] = max(done, last_hour)

    last_prune = prev.get("prune_ts", 0)
    if ts - last_prune > 3600:
        db.prune(conn, ts, cfg.raw_days, cfg.rollup_days)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        prev["_prune_ts_next"] = ts


def _upsert_topology(conn, cfg, ts):
    for dc in cfg.disks:
        conn.execute(
            """INSERT INTO disk(dev,role,model,serial,size_bytes,mount,fs,label,rotational,
                 pool,protected,monitored,maj_min,first_seen,last_seen)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(dev) DO UPDATE SET role=excluded.role, model=excluded.model,
                 serial=excluded.serial, size_bytes=excluded.size_bytes, mount=excluded.mount,
                 fs=excluded.fs, label=excluded.label, rotational=excluded.rotational,
                 pool=excluded.pool, protected=excluded.protected, monitored=excluded.monitored,
                 maj_min=excluded.maj_min, last_seen=excluded.last_seen""",
            (dc.dev, dc.role, dc.model, dc.serial, dc.size_bytes, dc.mount, dc.fs, dc.label,
             1 if dc.rotational else 0, dc.pool, 1 if dc.protected else 0,
             1 if dc.monitored else 0, dc.maj_min, ts, ts),
        )


def _pack_raw(cfg, ts, boot, rapl, core, pkg, busy, ds, procs_full, cgio,
              disk_state, prev, name_map, names_ts) -> dict:
    # compact procs to the minimum needed for next delta
    procs_compact = {
        str(pid): {
            "starttime": p["starttime"],
            "read_bytes": p["read_bytes"],
            "write_bytes": p["write_bytes"],
            "ticks": p["ticks"],
        }
        for pid, p in procs_full.items()
    }
    cgio_ser = {path: {dev: list(v) for dev, v in devs.items()} for path, devs in cgio.items()}
    return {
        "ts": ts,
        "boot_id": boot,
        "rapl": {k: list(v) for k, v in rapl.items()},
        "core": core,
        "pkg": pkg,
        "busy": busy,
        "diskstats": ds,
        "procs": {int(k): v for k, v in procs_compact.items()},
        "cgio": cgio_ser,
        "disk_state": disk_state,
        "container_names": name_map,
        "container_names_ts": names_ts,
        "awake_since": prev.get("_awake_since_next", prev.get("awake_since", {})),
        "stay_awake_ts": prev.get("_stay_awake_ts_next", prev.get("stay_awake_ts", {})),
        "power_event_ts": prev.get("_power_event_ts_next", prev.get("power_event_ts", {})),
        "prev_pkg_w": prev.get("_prev_pkg_w_next", prev.get("prev_pkg_w")),
        "overhead_ts": prev.get("_overhead_ts_next", prev.get("overhead_ts", 0)),
        "active_core_watts_ema": prev.get("_active_core_watts_next",
                                          prev.get("active_core_watts_ema",
                                                   _BUDGET_ASSUMED_WATTS_PER_CORE)),
        "rollup_hour": prev.get("_rollup_hour_next", prev.get("rollup_hour", 0)),
        "prune_ts": prev.get("_prune_ts_next", prev.get("prune_ts", 0)),
        "cycle": prev.get("_cycle_next", prev.get("cycle", 0)),
        "electricity": prev.get("_electricity_next", prev.get("electricity", {})),
        "collector_state": prev.get("_collector_state_next", prev.get("collector_state", {})),
    }
