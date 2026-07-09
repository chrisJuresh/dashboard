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
 systemd timer (20s)                         Cloudflare Access (SSO login)
        │                                              │
        ▼                                               ▼
  a3watch sample  ──►  SQLite (WAL) on NVMe  ◄──  a3watch API + SPA (socket-activated)
  (Python, root,       /var/lib/a3watch            served over your cloudflared tunnel
   short-lived)                                    at https://a3.chrisj.uk
```

- **Collector** (`agent/a3watch`): reads `/proc`, `/sys`, RAPL energy, package
  C-state MSRs, `cgroup v2 io.stat`, per-process `/proc/<pid>/io`; computes deltas
  vs. the previous cycle; runs attribution; appends to SQLite. **All data lives on
  the NVMe/SSD** (`/var/lib/a3watch`); the installer refuses a data dir backed by a
  rotational disk.
- **API + web** (`a3watch serve`): stdlib HTTP, socket-activated, read-only
  `SELECT`s. It serves the built SPA **and** its JSON API from one origin over your
  existing **cloudflared** tunnel. Auth is **Cloudflare Access** (SSO): the agent
  verifies the signed Access login token (a bearer token remains for direct/CLI use).
- **Dashboard** (`src/`): a static SvelteKit SPA (adapter-static) built locally and
  deployed into the agent's web dir on NVMe. Because UI + API share one origin
  behind Access, there's no CORS and nothing to enter — you log in via Cloudflare
  and land on the stats. (Not hosted on Vercel.)

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
#    data dir, tunnel hostname:
sudoedit /etc/a3watch/config.toml

# 3) apply: create data dir + systemd timer/socket, install (dormant) diagnostic tools
sudo a3watch install --confirm
```

Then serve the built SPA from the agent, route your cloudflared tunnel hostname to
`http://<host>:8787`, and put the hostname behind **Cloudflare Access** (set
`[access] team_domain` + `aud` in the config). You then visit the hostname, log in
via Cloudflare, and see the stats — no URL or token to enter.

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
