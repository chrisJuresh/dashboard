#!/usr/bin/env python3
"""
Auto-discovery tests: a drive added after install must show up on the dashboard
automatically, observed with the SAFEST default (protected=True → a3watch issues
it no command at all) until a human reviews it. Discovery is append-only and
non-waking (sysfs/proc only).

No external deps. Run: python3 agent/tests/test_discovery.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, PKG)

from a3watch import detect  # noqa: E402
from a3watch.config import Config, DiskCfg  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


print("== enumerate_disks is a plain sysfs read ==")
enum = detect.enumerate_disks()
check("returns a non-empty list on this host", isinstance(enum, list) and len(enum) > 0,
      f"got {enum!r}")
check("every entry maps to a real /sys/block device",
      all(os.path.isdir(f"/sys/block/{d['dev']}") for d in enum))
sys_devs = {d["dev"] for d in enum}

print("== discovery on an empty config finds every disk, safely ==")
cfg = Config()  # no disks configured
added = detect.discover_new_disks(cfg)
check("added one DiskCfg per system disk", {d.dev for d in added} == sys_devs,
      f"added={sorted(d.dev for d in added)} sys={sorted(sys_devs)}")
check("all flagged auto_detected", all(d.auto_detected for d in added))
check("all protected=True (no command issued to an unreviewed drive)",
      all(d.protected for d in added))
check("all role='unknown' (left for human review)", all(d.role == "unknown" for d in added))
check("all monitored=True (they show up)", all(d.monitored for d in added))
check("identity captured (serial or model present)",
      all((d.serial or d.model) for d in added))

print("== discovery is idempotent (a second pass adds nothing) ==")
again = detect.discover_new_disks(cfg)
check("second pass adds no duplicates", again == [], f"re-added {[d.dev for d in again]}")
check("disk count unchanged after re-run", len(cfg.disks) == len(sys_devs))

print("== append-only: a configured drive is left alone, not re-added ==")
one = enum[0]
cfg2 = Config()
cfg2.disks = [DiskCfg(dev=one["dev"], role="pool", serial=one["serial"],
                      protected=False, auto_detected=False)]
added2 = detect.discover_new_disks(cfg2)
check("the already-configured disk is not re-added",
      one["dev"] not in {d.dev for d in added2})
check("its human settings are untouched (role/protected preserved)",
      cfg2.disks[0].role == "pool" and cfg2.disks[0].protected is False
      and cfg2.disks[0].auto_detected is False)
check("the other disks are still discovered",
      {d.dev for d in added2} == (sys_devs - {one["dev"]}))

print("== match by serial even if the dev name differs (rename-safe) ==")
if one["serial"]:
    cfg3 = Config()
    cfg3.disks = [DiskCfg(dev="sdX_old", serial=one["serial"])]
    added3 = detect.discover_new_disks(cfg3)
    check("not re-added when the serial already matches",
          one["dev"] not in {d.dev for d in added3})
else:
    print("  (first disk has no serial — skipping serial-match check)")

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} check(s): {', '.join(FAILS)}")
    sys.exit(1)
print("All discovery checks passed.")
