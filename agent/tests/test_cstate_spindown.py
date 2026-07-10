#!/usr/bin/env python3
"""
Unit tests for the two honesty fixes:

  * C-state DEPTH must come from the MWAIT hint, not the ACPI state *name*.
    A box whose deepest state is named "C3_ACPI" can still reach hardware C10
    (MWAIT 0x60); name-matching wrongly reported "no deep C-states".
  * Scheduled sleep (a timer that unmounts + `hdparm -y` a drive) must be
    detected and reported SEPARATELY from an ATA idle timer, so a drive that
    sleeps on a clock is never flagged as an overdue idle timer.

No external deps. Run: python3 agent/tests/test_cstate_spindown.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, PKG)

from a3watch import power, disks  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


print("== MWAIT hint decode ==")
# the exact descs seen on a3server: C2_ACPI=0x21 is really C6, C3_ACPI=0x60 is C10
check("MWAIT 0x0 -> C1", power._cstate_from_desc("ACPI FFH MWAIT 0x0") == 1)
check("MWAIT 0x21 -> C6", power._cstate_from_desc("ACPI FFH MWAIT 0x21") == 6)
check("MWAIT 0x60 -> C10", power._cstate_from_desc("ACPI FFH MWAIT 0x60") == 10)
check("MWAIT 0x10 -> C3", power._cstate_from_desc("MWAIT 0x10") == 3)
check("MWAIT 0x30 -> C7", power._cstate_from_desc("MWAIT 0x30") == 7)
check("POLL has no hint", power._cstate_from_desc("CPUIDLE CORE POLL IDLE") is None)
check("empty desc -> None", power._cstate_from_desc("") is None)

print("== native-name fallback (only used when no MWAIT hint) ==")
check("name C6 -> 6", power._cstate_from_name("C6") == 6)
check("name C10 -> 10", power._cstate_from_name("C10") == 10)
check("name C1E -> 1", power._cstate_from_name("C1E") == 1)
check("name POLL -> None", power._cstate_from_name("POLL") is None)

print("== state ordering is numeric, not lexical ==")
check("state10 sorts after state2",
      sorted(["state0", "state10", "state2"], key=power._state_index) ==
      ["state0", "state2", "state10"])

print("== live box: cores reach deep C-states (the regression this guards) ==")
info = power.core_cstate_info()
check("core_cstate_info deep_available is True on this box",
      info.get("deep_available") is True, f"info={info}")
check("deepest decoded core state is C6+ (this box: C10)",
      info.get("deepest_num", 0) >= 6, f"deepest_num={info.get('deepest_num')}")

print("== scheduled-sleep parser (synthetic, mirrors a3-*-sleep scripts) ==")
# literal device on the hdparm line
check("hdparm -y /dev/sda -> sda",
      "sda" in disks._force_sleep_devices("sync\nhdparm -y /dev/sda\n"))
# VAR=/dev/... ; hdparm -y "$VAR"  (exactly how a3-hgst-sleep is written)
script = 'HGST="/dev/sdb"\nsync\numount /mnt/bay2\nhdparm -y "$HGST"\n'
check("VAR indirection hdparm -y \"$HGST\" -> sdb",
      "sdb" in disks._force_sleep_devices(script))
# a bare -C check power query must NOT be read as a force-sleep
check("hdparm -C is not a force-sleep",
      disks._force_sleep_devices("hdparm -C /dev/sda\n") == [])

print("== commented-out config is ignored ==")
check("_strip_comments drops # and ; lines",
      disks._strip_comments("live\n#dead\n  ; also dead\nkeep") == "live\nkeep")
check("commented hdparm.conf block not read as a force-sleep",
      disks._force_sleep_devices(disks._strip_comments(
          "#/dev/sda {\n#  hdparm -y /dev/sda\n#}\n")) == [])

print("== live box: idle-timer map has no unresolved by-id keys ==")
timers = disks.detect_spindown_timers()
check("all detected idle-timer keys resolve to kernel dev names (no ata-* junk)",
      all("/" not in k and not k.startswith("ata-") for k in timers),
      f"timers={timers}")

print("== systemd directive parsing ==")
svc = "[Service]\nExecStart=/usr/bin/flock -n /run/x.lock /usr/local/sbin/a3-maintenance\n"
check("ExecStart drops flock wrapper, keeps script",
      disks._scripts_from_execstart(svc) == {"/usr/local/sbin/a3-maintenance"})
tmr = "[Timer]\nOnCalendar=*-*-* 04:00:00\n"
check("OnCalendar parsed", disks._ini_values(tmr, "OnCalendar") == ["*-*-* 04:00:00"])

print("== detect_sleep_schedules returns well-formed data ==")
sched = disks.detect_sleep_schedules()
check("detect_sleep_schedules returns a dict", isinstance(sched, dict), f"got {type(sched)}")
shape_ok = all(isinstance(v, dict) and {"unit", "when", "how"} <= set(v) for v in sched.values())
check("every schedule entry has unit/when/how", shape_ok, f"sched={sched}")
# on a3server the backup (sdb) + parity (sde) are force-slept by a3-maintenance;
# informational, not a hard failure on other boxes
print(f"     (detected scheduled-sleep devices: {sorted(sched)})")

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} check(s): {', '.join(FAILS)}")
    sys.exit(1)
print("All C-state / spindown checks passed.")
