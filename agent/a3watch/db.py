"""
a3watch.db — SQLite schema, access helpers, rollups and retention.

All data lives in a single SQLite file on the NVMe/SSD (enforced by the
installer, which refuses a data dir backed by a rotational device). WAL mode
keeps writes cheap; the whole DB is opened per short-lived sampler run and
closed again, so there is no resident process holding it.

The schema is intentionally normalised and small. High-cardinality raw tables
are pruned after `raw_days`; hourly rollups are retained for `rollup_days`.
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, Iterable, Optional

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- static topology, refreshed by `detect`/`install` and lightly on each sample
CREATE TABLE IF NOT EXISTS disk (
    dev        TEXT PRIMARY KEY,   -- kernel name e.g. 'sda'
    role       TEXT,               -- system|pool|parity|backup|data|unknown
    model      TEXT,
    serial     TEXT,
    size_bytes INTEGER,
    mount      TEXT,
    fs         TEXT,
    label      TEXT,
    rotational INTEGER,            -- 1 HDD, 0 SSD/NVMe
    pool       TEXT,               -- e.g. 'a3media' if a mergerfs branch
    protected  INTEGER DEFAULT 1,  -- 1 => never issue hdparm -C to this drive
    monitored  INTEGER DEFAULT 1,
    maj_min    TEXT,               -- '8:0'
    first_seen REAL,
    last_seen  REAL
);

-- one row per sampling cycle: cadence + the tool's OWN overhead for that cycle
CREATE TABLE IF NOT EXISTS sample (
    ts          REAL PRIMARY KEY,
    interval_s  REAL,
    boot_id     TEXT,
    self_wall_ms REAL,
    self_cpu_ms  REAL,
    self_rss_kb  INTEGER,
    db_bytes     INTEGER
);

CREATE TABLE IF NOT EXISTS cpu_power (
    ts     REAL,
    domain TEXT,   -- package|core|uncore|dram
    watts  REAL,
    PRIMARY KEY (ts, domain)
);

CREATE TABLE IF NOT EXISTS cpu_busy (
    ts        REAL PRIMARY KEY,
    busy_pct  REAL,   -- 100 - idle%, from /proc/stat
    iowait_pct REAL,
    irq_pct   REAL
);

CREATE TABLE IF NOT EXISTS cstate (
    ts            REAL,
    scope         TEXT,   -- 'core' or 'package'
    name          TEXT,   -- C1/C6/... or PC2/PC6/PC8/PC10/S0ix
    residency_pct REAL,
    PRIMARY KEY (ts, scope, name)
);

-- top CPU movers in a cycle (for wattage / package-C-state attribution)
CREATE TABLE IF NOT EXISTS cpu_top (
    ts      REAL,
    pid     INTEGER,
    comm    TEXT,
    cgroup  TEXT,
    cpu_pct REAL,
    PRIMARY KEY (ts, pid)
);

CREATE TABLE IF NOT EXISTS disk_sample (
    ts          REAL,
    dev         TEXT,
    reads_d     INTEGER,   -- deltas over the cycle, from /proc/diskstats
    writes_d    INTEGER,
    rsect_d     INTEGER,
    wsect_d     INTEGER,
    io_ms_d     INTEGER,   -- ms the device had I/O in flight
    power_state TEXT,      -- active|idle|standby|unknown (passive or hdparm -C)
    active      INTEGER,   -- 1 if any physical I/O happened this cycle
    PRIMARY KEY (ts, dev)
);

CREATE TABLE IF NOT EXISTS disk_event (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL,
    dev           TEXT,
    kind          TEXT,   -- spinup|write|read|stay_awake
    confidence    TEXT,   -- high|medium|low
    primary_cause TEXT,   -- human string, best guess
    cause_kind    TEXT,   -- process|container|unit|kernel|filesystem|unknown
    note          TEXT
);
CREATE INDEX IF NOT EXISTS idx_disk_event_ts ON disk_event(ts);

CREATE TABLE IF NOT EXISTS event_evidence (
    event_id INTEGER,
    signal   TEXT,   -- e.g. 'proc_io','cgroup_io','unit_fire','diskstats'
    detail   TEXT,
    weight   REAL,
    PRIMARY KEY (event_id, signal, detail)
);

CREATE TABLE IF NOT EXISTS proc_io (
    ts           REAL,
    pid          INTEGER,
    comm         TEXT,
    cgroup       TEXT,
    read_bytes_d INTEGER,
    write_bytes_d INTEGER,
    PRIMARY KEY (ts, pid)
);

CREATE TABLE IF NOT EXISTS cgroup_io (
    ts       REAL,
    name     TEXT,    -- friendly container/service name
    dev      TEXT,    -- maj:min
    rbytes_d INTEGER,
    wbytes_d INTEGER,
    PRIMARY KEY (ts, name, dev)
);

CREATE TABLE IF NOT EXISTS unit_fire (
    ts      REAL,
    unit    TEXT,
    exec_ts REAL,
    PRIMARY KEY (ts, unit)
);

CREATE TABLE IF NOT EXISTS power_event (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL,
    kind          TEXT,   -- watt_rise|pkg_cstate_stall
    confidence    TEXT,
    primary_cause TEXT,
    detail        TEXT
);
CREATE INDEX IF NOT EXISTS idx_power_event_ts ON power_event(ts);

-- stray/unorganised process observations
CREATE TABLE IF NOT EXISTS proc_flag (
    ts    REAL,
    pid   INTEGER,
    comm  TEXT,
    cgroup TEXT,
    flag  TEXT,   -- crashloop|stray|poller|orphan
    note  TEXT,
    PRIMARY KEY (ts, pid, flag)
);

-- hourly rollups (kept longer than raw)
CREATE TABLE IF NOT EXISTS cpu_power_hourly (
    hour   INTEGER,   -- unix hour bucket (ts // 3600)
    domain TEXT,
    avg_w  REAL,
    max_w  REAL,
    PRIMARY KEY (hour, domain)
);

CREATE TABLE IF NOT EXISTS cstate_hourly (
    hour  INTEGER,
    scope TEXT,
    name  TEXT,
    avg_pct REAL,
    PRIMARY KEY (hour, scope, name)
);

CREATE TABLE IF NOT EXISTS disk_active_hourly (
    hour        INTEGER,
    dev         TEXT,
    active_cycles INTEGER,
    total_cycles  INTEGER,
    reads_sum   INTEGER,
    writes_sum  INTEGER,
    PRIMARY KEY (hour, dev)
);

-- current snapshot of every collector metric (small: one row per collector+key)
CREATE TABLE IF NOT EXISTS metric_latest (
    collector TEXT,
    grp       TEXT,
    key       TEXT,
    num       REAL,
    txt       TEXT,
    unit      TEXT,
    ts        REAL,
    PRIMARY KEY (collector, key)
);

-- bounded history, only for metrics flagged series=True (e.g. temps, fan rpm, rates)
CREATE TABLE IF NOT EXISTS metric_series (
    ts  REAL,
    key TEXT,     -- "collector.key"
    num REAL,
    PRIMARY KEY (ts, key)
);
CREATE INDEX IF NOT EXISTS idx_metric_series_key ON metric_series(key, ts);

CREATE TABLE IF NOT EXISTS metric_series_hourly (
    hour INTEGER,
    key  TEXT,
    avg_num REAL,
    min_num REAL,
    max_num REAL,
    PRIMARY KEY (hour, key)
);

CREATE TABLE IF NOT EXISTS overhead (
    ts        REAL PRIMARY KEY,
    cpu_ms_day REAL,
    avg_watts  REAL,
    gbp_year   REAL,
    db_bytes   INTEGER,
    samples    INTEGER
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    """Open (creating parent dir if needed) with pragmatic low-overhead pragmas."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=10000;")
    conn.execute("PRAGMA foreign_keys=OFF;")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    set_kv(conn, "schema_version", str(SCHEMA_VERSION))


# ---- key/value (persisted raw counters for delta computation, meta) --------
def set_kv(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO kv(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def get_kv(conn: sqlite3.Connection, key: str, default: Optional[str] = None) -> Optional[str]:
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_json(conn: sqlite3.Connection, key: str, obj: Any) -> None:
    set_kv(conn, key, json.dumps(obj, separators=(",", ":")))


def get_json(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    raw = get_kv(conn, key)
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


# ---- bulk insert helper ----------------------------------------------------
def insert_many(conn: sqlite3.Connection, table: str, cols: list[str], rows: Iterable[tuple]) -> None:
    rows = list(rows)
    if not rows:
        return
    placeholders = ",".join("?" * len(cols))
    collist = ",".join(cols)
    conn.executemany(
        f"INSERT OR REPLACE INTO {table} ({collist}) VALUES ({placeholders})",
        rows,
    )


# ---- rollups & retention ---------------------------------------------------
def rollup_hour(conn: sqlite3.Connection, hour: int) -> None:
    """Aggregate one completed unix-hour bucket into the *_hourly tables."""
    lo = hour * 3600
    hi = lo + 3600
    conn.execute(
        """INSERT OR REPLACE INTO cpu_power_hourly(hour,domain,avg_w,max_w)
           SELECT ?, domain, AVG(watts), MAX(watts) FROM cpu_power
           WHERE ts>=? AND ts<? GROUP BY domain""",
        (hour, lo, hi),
    )
    conn.execute(
        """INSERT OR REPLACE INTO cstate_hourly(hour,scope,name,avg_pct)
           SELECT ?, scope, name, AVG(residency_pct) FROM cstate
           WHERE ts>=? AND ts<? GROUP BY scope,name""",
        (hour, lo, hi),
    )
    conn.execute(
        """INSERT OR REPLACE INTO disk_active_hourly
              (hour,dev,active_cycles,total_cycles,reads_sum,writes_sum)
           SELECT ?, dev, SUM(active), COUNT(*), SUM(reads_d), SUM(writes_d)
           FROM disk_sample WHERE ts>=? AND ts<? GROUP BY dev""",
        (hour, lo, hi),
    )
    conn.execute(
        """INSERT OR REPLACE INTO metric_series_hourly(hour,key,avg_num,min_num,max_num)
           SELECT ?, key, AVG(num), MIN(num), MAX(num) FROM metric_series
           WHERE ts>=? AND ts<? GROUP BY key""",
        (hour, lo, hi),
    )


def prune(conn: sqlite3.Connection, now_ts: float, raw_days: int, rollup_days: int) -> None:
    raw_cut = now_ts - raw_days * 86400
    roll_cut_hour = int((now_ts - rollup_days * 86400) // 3600)
    for tbl in (
        "sample", "cpu_power", "cpu_busy", "cstate", "cpu_top",
        "disk_sample", "proc_io", "cgroup_io", "unit_fire", "proc_flag",
        "metric_series",
    ):
        conn.execute(f"DELETE FROM {tbl} WHERE ts < ?", (raw_cut,))
    # events kept for rollup_days (they are low-volume and high-value)
    ev_cut = now_ts - rollup_days * 86400
    conn.execute("DELETE FROM event_evidence WHERE event_id IN "
                 "(SELECT id FROM disk_event WHERE ts < ?)", (ev_cut,))
    conn.execute("DELETE FROM disk_event WHERE ts < ?", (ev_cut,))
    conn.execute("DELETE FROM power_event WHERE ts < ?", (ev_cut,))
    conn.execute("DELETE FROM overhead WHERE ts < ?", (ev_cut,))
    for tbl in ("cpu_power_hourly", "cstate_hourly", "disk_active_hourly", "metric_series_hourly"):
        conn.execute(f"DELETE FROM {tbl} WHERE hour < ?", (roll_cut_hour,))


def db_size_bytes(db_path: str) -> int:
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.path.getsize(db_path + suffix)
        except OSError:
            pass
    return total
