#!/usr/bin/env python3
"""
Device-identity stability tests: a3watch must follow a physical drive across a
sdX rename (Mark's point — add or pull a disk and sde can come back as sdf), by
resolving the kernel name from the drive's stable serial/WWN, not trusting the
name baked into the config.

No external deps. Run: python3 agent/tests/test_dev_resolve.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, PKG)

from a3watch import config  # noqa: E402
from a3watch.config import Config, DiskCfg  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


print("== stable_disk_id reads a cached (non-waking) identity ==")
smap = config._serial_dev_map()
check("_serial_dev_map returns a dict", isinstance(smap, dict))
check("every mapped id resolves to a real /sys/block entry",
      all(os.path.isdir(f"/sys/block/{d}") for d in smap.values()), f"map={smap}")
check("ids are non-empty strings", all(isinstance(s, str) and s for s in smap))

print("== remap: a stale config dev is corrected to the drive's current name ==")
if smap:
    real_id, real_dev = next(iter(smap.items()))
    cfg = Config()
    cfg.disks = [DiskCfg(dev="sdX_stale", serial=real_id, maj_min="99:99")]
    remaps = config.resolve_devs_by_serial(cfg)
    check("one remap reported", len(remaps) == 1, f"remaps={remaps}")
    check("dev corrected to current kernel name", cfg.disks[0].dev == real_dev,
          f"got {cfg.disks[0].dev}, want {real_dev}")
    check("maj_min re-read for the new dev (not the stale 99:99)",
          cfg.disks[0].maj_min != "99:99")
else:
    print("  (no SATA/PATA disks on this host — skipping live remap check)")

print("== no-op when the dev already matches (no false remaps) ==")
if smap:
    real_id, real_dev = next(iter(smap.items()))
    cfg = Config()
    cfg.disks = [DiskCfg(dev=real_dev, serial=real_id)]
    check("unchanged dev produces no remap", config.resolve_devs_by_serial(cfg) == [])

print("== disks without a stored serial are left alone (older configs) ==")
cfg = Config()
cfg.disks = [DiskCfg(dev="sda", serial="")]
check("empty-serial disk is a no-op", config.resolve_devs_by_serial(cfg) == [])
check("empty-serial disk keeps its dev", cfg.disks[0].dev == "sda")

print("== unknown serial (drive absent) is left alone, not blanked ==")
cfg = Config()
cfg.disks = [DiskCfg(dev="sdz", serial="naa.deadbeefnotpresent")]
check("absent-serial disk is a no-op", config.resolve_devs_by_serial(cfg) == [])
check("absent-serial disk keeps its configured dev", cfg.disks[0].dev == "sdz")

print("== a pulled drive (serial absent from system) is marked not-present ==")
if smap:
    cfg = Config()
    cfg.disks = [DiskCfg(dev="sdgone", serial="naa.deadbeefpulled")]
    config.resolve_devs_by_serial(cfg)
    check("absent-serial drive flagged present=False", cfg.disks[0].present is False)

print("== pull+rename collision: cfg.disk() returns the PRESENT entry, not the ghost ==")
if smap:
    real_id, real_dev = next(iter(smap.items()))
    cfg = Config()
    # ghost: a removed drive whose stale entry still names `real_dev`, protected=False
    ghost = DiskCfg(dev=real_dev, serial="naa.deadbeefpulled", protected=False)
    # live: the surviving drive that now occupies `real_dev`, operator set protected=True
    live = DiskCfg(dev="sdX_old", serial=real_id, protected=True)
    cfg.disks = [ghost, live]  # ghost first — the dangerous ordering
    config.resolve_devs_by_serial(cfg)
    check("ghost marked not-present", ghost.present is False)
    check("live remapped onto the shared dev", live.dev == real_dev and live.present)
    got = cfg.disk(real_dev)
    check("cfg.disk() returns the PRESENT (live) entry", got is live,
          f"returned {'live' if got is live else 'ghost' if got is ghost else None}")
    check("so the live drive's protected=True is honoured (no command bypass)",
          got.protected is True)

print("== dev + maj_min stay in lockstep on remap ==")
if smap:
    real_id, real_dev = next(iter(smap.items()))
    cfg = Config()
    cfg.disks = [DiskCfg(dev="sdX_old", serial=real_id, maj_min="99:99")]
    config.resolve_devs_by_serial(cfg)
    check("maj_min no longer the stale 99:99 after dev remap",
          cfg.disks[0].maj_min != "99:99")

print("== the writer persists serial (so the stable id survives a rewrite) ==")
cfg = Config()
cfg.disks = [DiskCfg(dev="sda", serial="naa.1234", model="X")]
check("dumps() emits the serial line", 'serial = "naa.1234"' in config.dumps(cfg))

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} check(s): {', '.join(FAILS)}")
    sys.exit(1)
print("All device-resolution checks passed.")
