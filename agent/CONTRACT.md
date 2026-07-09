# a3watch build contract

This file is the **fixed interface** every agent module and the frontend must
build against. Foundation modules (`util.py`, `db.py`, `config.py`) are already
written — **read them before writing anything**. Do not change their public
surface; build on it.

## Absolute safety rules (violating these is a defect)

1. **Never open a block device** (`/dev/sd*`, `/dev/nvme*`) for read or write.
   Only read `/proc` and `/sys` pseudo-files, via `util.read_text/read_int/...`.
2. In normal (non-diagnostic) operation the **only** command that may touch a
   disk is `util.hdparm_power_state(dev)` (ATA CHECK POWER MODE, non-waking),
   and only for disks whose `DiskCfg.protected is False` **and** when
   `cfg.use_hdparm_c` is True. Everything heavier (`smartctl -a`, `blktrace`,
   `bpftrace`, `biosnoop`, raw reads, `dd`) is **diagnostic-only**, lives in
   `diag.py`, runs `util.run_cmd(..., allow_diagnostic=True)`, is time-boxed,
   and only starts on an explicit authenticated request.
3. **Observe only.** No module may change disk/RAID/filesystem/SMART/CPU
   governor/C-state/APM/spindown/power settings. No `hdparm -S/-B/-y`, no
   `cpupower ... set`, no writes to `/sys/.../scaling_governor`, etc. Tuning is
   surfaced as printed text recommendations only.
4. Every reader tolerates missing files / EPERM and returns a neutral value;
   a sampling cycle must never crash on a moved counter.
5. All writable paths are under `cfg.data_dir` (NVMe). Nothing writes to an HDD.

## Foundation API already available

- `util`: `read_text/read_first_line/read_int/list_dir/path_exists`, `now/monotonic/boot_id`,
  `run_cmd(argv, timeout, allow_diagnostic=False)`, `have_cmd(name)`,
  `hdparm_power_state(dev)`, `is_rotational(dev)`, `block_devices()`,
  `backing_block_device(path)`, `dev_maj_min(dev)`.
- `db`: `connect(db_path)`, `init_schema(conn)`, `set_kv/get_kv/set_json/get_json`,
  `insert_many(conn, table, cols, rows)`, `rollup_hour(conn, hour)`,
  `prune(conn, now_ts, raw_days, rollup_days)`, `db_size_bytes(db_path)`. Schema in `db.py`.
- `config`: `Config`, `DiskCfg`, `load(path)`, `save(cfg, path)`, `dumps(cfg)`,
  `validate(cfg)`, `validate_data_dir_nonrotational(dir)`. `Config.db_path/token_path/diag_dir`.

## Modules to implement (each owns its file)

### `power.py`
- `read_rapl_energy() -> dict[str, tuple[int,int]]` — domain -> (energy_uj, max_range_uj) from
  `/sys/class/powercap/intel-rapl:*`. Needs root; return {} if unreadable.
- `rapl_watts(prev, cur, dt) -> dict[str,float]` — per-domain watts from energy deltas,
  handling counter wrap via max_range_uj. Domains named package/core/uncore/dram.
- `read_core_cstates() -> dict[str, dict[str,int]]` — per-cpu {state_name: usage,time_us} from
  `/sys/devices/system/cpu/cpu*/cpuidle/state*/{name,usage,time}`.
- `core_cstate_residency(prev, cur, dt) -> list[tuple[str,float]]` — aggregate residency % per
  state across all CPUs (time delta / (dt*ncpu)*100). POLL/C1/C6...
- `read_pkg_cstates() -> dict[str,int]` — package C-state residency counters from
  `/sys/kernel/debug/pmc_core/package_cstate_show` and `slp_s0_residency_usec` if present;
  else {} (report "package C-states need diagnostic turbostat").
- `pkg_cstate_residency(prev, cur, dt) -> list[tuple[str,float]]`.
- `read_cpu_busy() -> dict` — from `/proc/stat` cpu line: return raw jiffies dict.
- `cpu_busy_pct(prev, cur) -> tuple[float,float,float]` — (busy%, iowait%, irq%).

### `disks.py`
- `read_diskstats() -> dict[str, dict]` — per whole-disk from `/proc/diskstats`:
  {reads, rsect, writes, wsect, io_ms}. Whole disks only (skip partitions).
- `diskstats_delta(prev, cur) -> dict[str,dict]` — per-dev deltas (>=0; reset→0).
- `passive_power_state(dev) -> str` — from `/sys/block/<dev>/device/power/runtime_status`
  plus `/sys/class/scsi_disk`/link-power hints; returns active|idle|standby|unknown WITHOUT
  any command. (runtime PM is often "active" for SATA; treat unknown gracefully.)
- `power_state(cfg, dev) -> str` — if cfg.use_hdparm_c and not protected → `util.hdparm_power_state`;
  else `passive_power_state`. Non-waking either way.

### `procs.py`
- `read_proc_io() -> dict[int, dict]` — for each /proc/<pid>: {comm, cgroup, read_bytes,
  write_bytes, utime, stime, starttime, ppid, state}. read_bytes/write_bytes from /proc/<pid>/io
  (physical block bytes, not cache). Skip pids we can't read.
- `proc_io_delta(prev, cur) -> list[dict]` — per-pid deltas for pids present in both with same
  starttime (guards pid reuse): {pid, comm, cgroup, read_bytes_d, write_bytes_d, cpu_ms_d}.
- `read_cgroup_io() -> dict[str, dict[str,tuple[int,int]]]` — per cgroup name -> {maj:min:(rbytes,wbytes)}
  from cgroup v2 `io.stat`. Map cgroup paths to friendly container/service names
  (docker: `/system.slice/docker-<id>.scope` → container name via /proc or label file;
  best-effort, fall back to last path component).
- `cgroup_io_delta(prev, cur) -> list[dict]`.
- `flag_stray(procs, container_states) -> list[dict]` — detect crashloop (container Restarting /
  short-lived respawns), stray (high cpu with no service cgroup / orphaned ppid=1 non-service),
  poller (periodic wakeups). Returns [{pid,comm,cgroup,flag,note}].

### `attribute.py` — the confidence engine
- `detect_disk_events(cfg, window) -> list[Event]` where `window` bundles this cycle's
  disk deltas+states, proc_io deltas, cgroup_io deltas, unit fires, prev disk states.
  A `spinup` = disk went standby→active OR (was standby last cycle and shows reads/writes now).
  A `stay_awake` = rotational disk still not in standby with no attributable recent I/O.
- `score(event, evidence) -> (confidence, primary_cause, cause_kind)` per the rules:
  HIGH = single process/container with matching physical I/O to that dev's mount, or a known
  scheduled unit fired; MEDIUM = multiple candidates / container-level only / mergerfs branch
  ambiguity; LOW = only kernel threads (kworker/flush/jbd2/md) → writeback/journal, origin unknown.
  Always attach the full evidence list. Be explicit about mergerfs/SnapRAID/cache uncertainty.
- `attribute_power(window) -> list[PowerEvent]` — watt_rise and pkg_cstate_stall (PC6/PC8≈0)
  attributed to top CPU/IRQ contributors, with confidence.
- An `Event`/`PowerEvent` is a plain dict matching the `disk_event`/`power_event`+`event_evidence`
  columns; `sample.py` persists them.

### `sample.py` — one cycle (the systemd timer target). MUST be cheap and non-waking.
- `run_once(cfg) -> dict` : open db; load prev raw (`db.get_json(conn,'last_raw')`); read all
  current raw (power, disks, procs, cgroup, cstates, unit fires, boot_id); if boot_id changed or
  no prev → store raw and skip deltas this cycle; else compute deltas, run attribute.*, insert rows
  (`sample`,`cpu_power`,`cpu_busy`,`cstate`,`cpu_top`,`disk_sample`,`disk_event`+evidence,`proc_io`,
  `cgroup_io`,`unit_fire`,`power_event`,`proc_flag`); update disk topology last_seen; save current
  raw; do hourly rollup for any newly-completed hour; prune per retention on a slow cadence
  (e.g. once/hour). Record self overhead: wall ms, `resource.getrusage` cpu ms, rss, db bytes.
  Wrap the whole body so one bad reader never aborts the cycle. Return a summary dict.
- Reading unit fires: `systemctl show <timer/service> -p InactiveEnterTimestamp/ActiveEnterTimestamp`
  for the timers detected; also read a3-maintenance/snapraid/scrutiny hints. Cheap.

### `detect.py` — topology detection (non-waking) + config builder
- `detect() -> dict` : gather NVMe/HDD classification (sysfs rotational), device model/serial/size
  (`/sys/block/<d>/device/{model,serial}`, `/size`), mounts+fs+labels (`findmnt`, `/etc/fstab`
  labels, `/dev/disk/by-label`), mergerfs branches (parse `/proc/mounts` fuse.mergerfs + fstab),
  snapraid roles (`/etc/snapraid.conf` parity/data), docker containers (`docker ps` best-effort,
  no failure if absent), RAPL domains, pmc_core availability, cpuidle states, tool availability,
  systemd timers likely to touch disks. Everything read-only & non-waking.
- `build_config(detection, existing: Config|None) -> Config` : classify each HDD role
  (system for NVMe, parity if label endswith 'par'/in snapraid parity, backup if 'bak',
  pool if a mergerfs branch, else data), monitored=True. `protected` defaults per mode:
  when use_hdparm_c is enabled, rotational drives default to protected=False so the
  chosen non-waking hdparm -C probing is active (flip to True to exclude a drive);
  when disabled, rotational drives default to protected=True. Preserve user edits from
  `existing` where present.

### `api.py` — read-only JSON API + CORS + bearer auth + fallback page
Implements the HTTP contract below using `http.server` (stdlib), socket-activated
(systemd passes the listening fd via `$LISTEN_FDS`; fall back to binding cfg.api_bind:port).
Idle-exit after `cfg.api_idle_exit_s`. Auth: `Authorization: Bearer <token>` compared to
`cfg.token_path` contents (constant-time). CORS: reflect origin if in `cfg.allow_origins`.
All handlers are SELECT-only; no handler probes disks. Diagnostic endpoints call diag.py and
require auth; they are the only POST endpoints.

### `diag.py` — diagnostic mode (explicit, gated, may add overhead / wake disks)
- `start(cfg, tool, seconds, dev=None) -> session_id` for tool in {biosnoop, ext4slower,
  bpftrace_bio, blktrace, turbostat, powertop, smart}. Runs the tool time-boxed via
  `util.run_cmd(..., allow_diagnostic=True)`, writes result JSON to cfg.diag_dir. `smart` and any
  waking read requires `confirm_wake=True`. `status()`, `result(session_id)`, `list_sessions()`.
- Refuse to start if binary missing; report which are installed.

### `cli.py` — `a3watch <cmd>`
Commands: `detect [--data-dir] [--dry-run]` (write config + print summary, no changes),
`install --confirm` (validate, create data dir, write systemd units, enable timer+socket,
apt-install diag tools, print tunnel snippet + uninstall command),
`sample` (run_once; the timer target), `serve` (run api; the socket service target),
`status` (print current snapshot to terminal), `diag <tool> ...`, `uninstall`,
`version`. Never makes system changes without `install --confirm`/`uninstall`.

## HTTP/JSON contract (frontend builds to exactly these)

Base path `/api`. All responses JSON. Errors: `{ "error": "msg" }` with proper status.
`GET /api/health` (no auth) → `{ok:true, version, ts, mode:"normal"|"diagnostic"}`.
All others require `Authorization: Bearer <token>`.

- `GET /api/status` → 
  `{ ts, mode, disks:[{dev,role,model,mount,label,rotational,protected,power_state,active,
     minutes_in_state,reads_recent,writes_recent}],
    cpu:{ pkg_w, core_w, busy_pct, pkg_cstates:[{name,pct}], core_cstates:[{name,pct}],
          pkg_deep_ok:bool },
    overhead:{ avg_watts, gbp_year, budget_gbp, db_mb, samples, cpu_ms_day, within_budget:bool },
    counts:{ open_disk_events, stray_procs } }`
- `GET /api/disks/events?since=<ts>&limit=<n>&dev=<dev>` →
  `{ events:[{id,ts,dev,kind,confidence,primary_cause,cause_kind,note,
      evidence:[{signal,detail,weight}]}] }`
- `GET /api/timeseries/power?from=&to=&res=raw|hour` → `{ points:[{ts,pkg_w,core_w,uncore_w,dram_w}] }`
- `GET /api/timeseries/cstate?from=&to=&scope=package|core&res=` →
  `{ scope, states:[name...], points:[{ts, <name>:pct, ...}] }`
- `GET /api/timeseries/disk?dev=&from=&to=&res=` →
  `{ dev, points:[{ts, active, reads_d, writes_d, power_state}] }`
- `GET /api/processes` → `{ procs:[{pid,comm,cgroup,cpu_pct,read_bytes_d,write_bytes_d,
      flags:[...],note}] }` (current interesting/stray set)
- `GET /api/power/events?since=&limit=` → `{ events:[{id,ts,kind,confidence,primary_cause,detail}] }`
- `GET /api/overhead?from=&to=` → `{ points:[{ts,avg_watts,gbp_year,db_bytes,samples,cpu_ms_day}],
      current:{...}, budget_gbp }`
- `GET /api/config` → sanitised: `{ interval_s, budget_gbp_year, data_dir, disks:[{dev,role,label,
      mount,rotational,protected,monitored,pool}], tunnel_hostname, mode }` (NO token)
- `POST /api/diag/start` (auth) body `{tool,seconds,dev?,confirm_wake?}` → `{session_id}`
- `GET /api/diag/status` → `{running:bool, sessions:[{id,tool,started,ends,dev}]}`
- `GET /api/diag/result/<id>` → `{id,tool,lines:[...],summary,started,ended}`

## Frontend (SvelteKit, adapter-static, deploys to Vercel)
SPA that reads the API over the cloudflared tunnel. First-run screen collects API base URL +
bearer token (stored in localStorage). Pages: Overview, Disks (events timeline + evidence drawer),
Power & C-states, Processes (stray), Overhead (£/yr vs budget), Diagnostics (gated), Settings.
Charts per the `dataviz` skill; theme-aware light/dark; slow polling at the sample cadence; the UI
must never trigger disk/turbostat activity — it only reads collected rows. Handle "agent
unreachable" (e.g. when viewed on Vercel with no tunnel) with a clear demo/empty state.
