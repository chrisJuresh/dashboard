# a3watch

A **measurement-first, low-power** observability tool for a Linux storage server.
It answers four questions **without becoming part of the problem**:

1. **What woke my HDDs?** — per spin-up, the likely cause with evidence and a confidence level.
2. **What's blocking deep CPU (package) C-states?**
3. **What's raising wattage?**
4. **Which processes are stray, crash-looping, or polling?**

It is built for a home/media server whose HDDs are meant to spin down to save power
and extend disk life. Crucially, **a3watch does not spin disks up during normal
operation, and never changes any system setting** — it only observes.

---

## Why this exists (and why it's not Prometheus/Grafana/scrutiny)

A normal monitoring stack (an always-on agent scraping every few seconds, a TSDB
like InfluxDB/Prometheus writing continuously) has real idle overhead and, worse,
tools like SMART pollers *actively wake the very disks you're trying to keep
asleep*. a3watch is the opposite:

- **No always-on daemon.** Sampling is a short-lived `systemd` timer job (default
  every 20 s). Between samples there is **zero resident process and ~0 W**.
- **No runtime dependencies.** The agent is pure Python 3 standard library
  (SQLite via the built-in module). Nothing to keep patched, tiny attack surface.
- **Socket-activated API.** The read-only web API uses **zero** resources until you
  open the dashboard, then idle-exits again.
- **Measured, not assumed, overhead.** The dashboard shows a3watch's *own* CPU
  time, wattage, DB size and projected £/year against a **£5/yr budget**. On the
  reference server it measures **≈ £0.004–0.02 / year**.

## Architecture

```
 systemd timer (20s)                    cloudflared tunnel
        │                                       │
        ▼                                        ▼
  a3watch sample  ──►  SQLite (WAL) on NVMe  ◄── a3watch API (socket-activated, read-only)
  (Python, root,       /var/lib/a3watch           │  bearer-token + CORS
   short-lived)                                     ▼
                                          SvelteKit SPA (static, hosted on Vercel)
```

- **Collector** (`agent/a3watch`): reads `/proc`, `/sys`, RAPL energy, package
  C-state MSRs, `cgroup v2 io.stat`, per-process `/proc/<pid>/io`; computes deltas
  vs. the previous cycle; runs attribution; appends to SQLite. **All data lives on
  the NVMe/SSD** (`/var/lib/a3watch`); the installer refuses a data dir backed by a
  rotational disk.
- **API** (`a3watch serve`): stdlib HTTP, read-only `SELECT`s, bearer auth, CORS
  limited to your Vercel origin, exposed to the internet only via your existing
  **cloudflared** tunnel.
- **Dashboard** (`src/`): a static SvelteKit SPA built and hosted by Vercel. Your
  browser loads it from Vercel and reads the agent's API over the tunnel — so
  **no data leaves the server except the query results you request**.

## Safety guarantees (enforced in code, verified on-box)

- **Non-waking.** Normal mode never opens a block device and never issues a
  disk-spinning command. Disk activity comes from `/proc/diskstats` counters; the
  only disk-touching call is `hdparm -C` (ATA *CHECK POWER MODE*, spec-guaranteed
  not to spin a drive up), used only for drives you have not marked `protected`.
  *Verified:* running full sample cycles left every standby disk in standby.
- **Observe-only.** Nothing changes governor, C-states, APM/spindown, SMART, RAID,
  filesystem or power settings. There is no auto-tuning path; tuning ideas are
  printed as suggestions you run yourself.
- **Data on NVMe only.** Enforced by a rotational-backing-device check at install.
- **Honest attribution.** Every spin-up event carries a confidence level and the
  raw evidence. Where block-level signals genuinely can't prove a cause —
  SMART/ATA passthrough (e.g. `scrutiny`), mergerfs file→branch ambiguity, kernel
  writeback/journal — a3watch says so and drops confidence rather than guessing.

## Install (two-step, review-gated)

```bash
# 1) place the agent + detect topology (makes NO systemd/pkg changes, wakes no disk)
sudo ./agent/install.sh

# 2) REVIEW the generated config — roles, which drives are 'protected', interval,
#    data dir, [api].allow_origins (your Vercel URL), tunnel hostname:
sudoedit /etc/a3watch/config.toml

# 3) apply: create data dir + systemd timer/socket, install (dormant) diagnostic tools
sudo a3watch install --confirm
```

Then expose the API through your cloudflared tunnel (the installer prints the exact
ingress snippet and your bearer token), add your Vercel URL to `allow_origins`, and
enter the API URL + token once on the dashboard's connect screen.

Handy commands: `a3watch status` (live, non-waking snapshot), `a3watch detect`
(re-detect), `sudo a3watch uninstall --confirm` (remove everything).

## Diagnostic mode (explicit, opt-in)

For gold-standard "which PID/file woke this disk" evidence, `a3watch diag` (or the
Diagnostics page) runs time-boxed tracers — `biosnoop`, `ext4slower`, `bpftrace`,
`btrace`, `turbostat`, `powertop`. These add overhead and are **off by default**.
The only tool that can spin up a standby disk (`smartctl -a`) requires an explicit
second confirmation (`--confirm-wake`).

## The reference server (auto-detected)

NVMe system drive (KIOXIA 512 GB) + 5 SATA HDDs pooled with **mergerfs**
(`/srv/a3/media`) and protected by **SnapRAID** (parity on the 14 TB, data on the
others), plus a backup drive. No block RAID (`md`/LVM). Docker stack incl. a
crash-looping Jellyfin and a SMART-polling `scrutiny` — both surfaced by a3watch.

## Power budget

UK ≈ £0.25/kWh → £5/yr ≈ **2.3 W continuous** ceiling. Design target < 0.2 W. The
agent's own measured overhead (shown on the Overhead page) is dominated by ~25 ms
of CPU per 20 s cycle ⇒ single-digit **milliwatts** — three orders of magnitude
under budget.

## Layout

```
agent/a3watch/    Python agent (util, db, config, power, disks, procs, attribute,
                  sample, detect, api, diag, cli)
agent/install.sh  bootstrap installer (step 1)
agent/CONTRACT.md module + HTTP/JSON contract
src/              SvelteKit dashboard (routes + $lib components/charts)
```
