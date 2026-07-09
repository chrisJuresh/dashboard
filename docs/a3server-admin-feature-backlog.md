# Feature backlog — ideas from `a3server-admin`

A catalogue of capabilities in a sibling private repo (`MarkJ1234/a3server-admin`) captured as
**candidate future features for a3watch**. Recorded 2026-07-09.

This is **feature-level only** — no drive serials, IPs, usernames, or config secrets from that
private repo are reproduced here; those stay in his repo. Nothing here judges his implementation
quality; it's a "what could we borrow" list.

## How the two projects differ (why the gaps cluster)

- **`a3server-admin`** is a *control + runbook* toolkit (bash + systemd): it **acts** on the
  server — spins drives up/down, controls the HDD fan, runs scheduled maintenance — and documents
  the setup. No power/C-state monitoring, no UI.
- **a3watch** is *observe-only + low-power*: it **measures and attributes** and changes nothing.

So most gaps are either (A) observability we could add cleanly, or (B) control actions that would
need an explicit, gated "actions" module (deferred by decision — see end).

**Legend:** 🟡 gap that fits a3watch (observe-only, low-power) · 🔵 control/action (needs an opt-in
actions module) · ⚪ out of scope for a3watch · 🟢 already in a3watch.

---

## A. Observability features to consider (fit a3watch)

### A1. 🟡 HDD temperature monitoring (activity-gated) — **high priority**
- **His approach:** fan controller reads HDD temps from SMART attributes (190/194) but only for a
  drive that kernel I/O counters (`/sys/block/*/stat`) show is **already active**; sleeping drives
  are skipped, with a cooldown window after activity. Sleeping drives report no temp (accepted).
- **a3watch today:** collects **no** temperatures at all.
- **How we'd add it (non-waking):** reuse the exact activity gate we already have — only read SMART
  temp for a drive whose diskstats moved this cycle (or is not in standby per `hdparm -C`). Store
  `disk_temp(ts, dev, celsius)`; show current + history on a new "Temps" view and per-disk card.
  Sleeping drives show "asleep — no temp" honestly. Biggest single gap for a spinning-rust box.

### A2. 🟡 Fan RPM + board/hwmon telemetry
- **His approach:** reads fan tach input and Super-I/O sensor chip via `/sys/class/hwmon`.
- **a3watch today:** notes `sensors` is available but surfaces nothing.
- **How:** read hwmon `fan*_input` + `temp*_input` (pure sysfs, ~free). Add to the Temps view.

### A3. 🟡 "NVMe silent-fill" hazard detector — **high priority, high value**
- **His writeup documents a real incident:** a pool mountpoint existed as a plain directory while
  the drive was unmounted, so a network copy silently wrote to the **OS NVMe** instead of the HDD;
  NVMe usage spiked. His mitigation is a human "golden rule" (always `findmnt` before copying).
- **a3watch today:** doesn't check for this.
- **How:** for each fstab entry with `nofail`, if the mount is **absent** but the mountpoint
  directory is **non-empty and backed by the root/NVMe device**, raise a high-severity alert
  ("writes here are landing on the NVMe, not the intended disk"). Directly serves "what's filling
  my NVMe." Pure sysfs/stat, non-waking.

### A4. 🟡 SnapRAID status surfacing
- **His approach:** `a3-maintenance` runs `snapraid sync` + `snapraid status`; results only in logs.
- **a3watch today:** detects SnapRAID topology and correlates the maintenance unit firing, but does
  not read status.
- **How:** parse `snapraid status` output (read-only; runs against content files, not a scrub) to
  show last sync/scrub time, files/blocks behind, errors, parity coverage. Note: `snapraid status`
  reads content files on data disks — schedule it to piggyback on maintenance windows so it doesn't
  itself wake sleeping disks.

### A5. 🟡 Per-drive spindown / APM timer reporting
- **His approach:** sets a drive's own idle spindown timer (`hdparm -S`).
- **a3watch today:** observe-only (correctly won't *set* it) but also doesn't *report* it, which is
  exactly the "why won't this drive sleep?" context we should show.
- **How:** report each drive's configured standby/APM settings. Reading these authoritatively needs
  care (`hdparm -I` **wakes** the disk — never use in normal mode); prefer reporting the *intended*
  settings from any spindown unit/config we detect, and only confirm live values in diagnostic mode.

### A6. 🟡 Continuous file-access "wake audit" (auditd) — complements our eBPF
- **His approach:** installs kernel **auditd** watch rules on drive paths, refreshed after mount
  changes (`a3-wake-audit-rules`), giving an always-on log of what touched watched paths.
- **a3watch today:** cheap continuous signals (diskstats/cgroup/proc) every 20s + **on-demand**
  eBPF; nothing catches the exact culprit *between* samples.
- **How:** optional always-on wake trail — either auditd watch rules or a lightweight persistent
  eBPF ring buffer on `block_rq_issue` filtered to spun-down disks — to fill the sub-sample gap.
  Must be weighed against the power budget (a persistent tracer is not free); could be
  "enhanced monitoring" toggle rather than default.

### A7. 🟡 Config snapshot + drift detection
- **His approach:** `snapshot.sh` copies live `fstab`, `snapraid.conf`, `smb.conf`, unit files and
  generates `lsblk`/`findmnt`/unit manifests into git; `commit-a3.sh` reviews + commits.
- **a3watch today:** stores nothing about system config.
- **How:** periodic non-waking snapshot of key config files to the NVMe data dir, with a diff/drift
  view ("fstab changed 3 days ago"). Backups + drift alerting, no external dependency.

### A8. 🟡 Temperature / health alerting
- **His approach:** fan loop has an emergency-temp failsafe (ramp to 100% at a threshold).
- **a3watch today:** no thresholds or alerts anywhere.
- **How:** once temps exist (A1/A2), add threshold alerts (drive/board temp, and reuse for
  SMART-health/NVMe-fill). Surface on dashboard; optional push later.

### A9. 🟡 SMART health summary (non-waking)
- **His approach:** runs `scrutiny` (SMART dashboard) in Docker, with its collector cron neutered to
  yearly so it stops waking disks; refreshed manually during maintenance.
- **a3watch today:** SMART only as an **opt-in diagnostic** snapshot (`diag smart`, requires
  confirm-wake); no health dashboard.
- **How:** surface a health summary for **awake** drives only (reallocated/pending/CRC counts),
  refreshed opportunistically when a drive is already up, or by reading scrutiny's stored data if
  present. Never wake a drive for SMART in normal mode.

### A10. 🟡 Event-driven drive active↔standby state watcher
- **His approach:** a persistent `a3-drive-state-watch` service (currently *disabled* on his box).
- **a3watch today:** samples power state every ~20s (timer).
- **How:** optional event-driven state-change log for finer transition timestamps than the sample
  interval; overlaps with A6. Low priority given sampling already covers most cases.

---

## B. Control / action features — **deferred (no actions module for now)**

a3watch is observe-only by design; these would require an explicit, per-action-confirmed "actions"
module. User decision (2026-07-09): **not now.** Listed so we don't lose them.

- **B1. 🔵 On-demand drive sleep/wake** — sleep = `sync` + busy-check + `umount` + `hdparm -y`;
  wake = `mount` (+ re-arm audit watches). He has this for the backup and parity drives.
- **B2. 🔵 HDD fan control** — activity-gated SMART-temp-driven PWM curve with piecewise-linear
  interpolation, gradual ramp-down / instant ramp-up smoothing, emergency-temp + bad-read failsafe,
  "all sleeping" low floor, hwmon auto-discovery by chip name.
- **B3. 🔵 Maintenance orchestration** — daily `flock`-guarded job: wake backup+parity → `snapraid
  sync`/`status` → refresh SMART → sleep them again.
- **B4. 🔵 Set per-drive spindown timer** (`hdparm -S`) as a one-shot unit.
- **B5. ⚪ Samba share management** — incl. the trick of marking the backup share `available = no`
  so browsing can't wake the backup drive. (Server config, not a3watch's job — but a good
  *recommendation* a3watch could surface.)
- **B6. ⚪ Future (neither tool has yet):** Wake-on-LAN, S3/suspend, a midnight idle-check
  sleep service.

---

## C. Runbook / documentation features

- **C1. 🟡 Living runbook / drive-map + incident log** — he keeps `DRIVE-MAP.md`, a storage
  writeup, "golden rules," and a log of past incidents (accidental NVMe fill, a transient SATA/ext4
  read-only event). a3watch auto-detects topology (Settings page) but has no notes/incident space.
  Consider a lightweight, editable "notes" panel or exporting detected topology as a drive-map.

---

## D. Already covered by a3watch (his toolkit lacks these) — for reference

Live **wattage (RAPL)** · **CPU package/core C-state** residency + stall attribution · **per-process
and per-container spin-up attribution with confidence + evidence** · **self-overhead / £-budget
accounting** · **web dashboard + read-only API** · **opt-in eBPF/turbostat diagnostics**. His
"understand what woke a disk" is an auditd log read by hand; ours is attributed automatically.

---

## E. Pointers & validating insights

- **Bodies not captured in his repo** (only their `.service` units are): `a3-drive-state-watch` and
  `a3-wake-audit-rules` live at `/usr/local/sbin/*` on the server. Read those on the live box before
  implementing A6/A10 to match his exact behaviour.
- **His findings validate a3watch's purpose:** the fan controller polling SMART every 15s was
  keeping the overflow HDD awake; scrutiny's collector was neutered to yearly to stop it waking
  disks; his stated strategy — "monitor active disks with SMART, idle disks with cheap kernel I/O
  counters" — is exactly a3watch's model. Detecting "a poller is holding this disk awake" is our
  core job.

## Suggested first picks (if/when we implement)
1. **A1 HDD temperatures (activity-gated)** — highest value, cleanly non-waking, reuses our gate.
2. **A3 NVMe silent-fill detector** — prevents a real, already-experienced data/space incident.
3. **A2 fan/board telemetry** and **A5 spindown reporting** — cheap context that rounds out "why
   won't this sleep / is it too hot."
