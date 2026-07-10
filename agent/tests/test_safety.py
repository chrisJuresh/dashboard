#!/usr/bin/env python3
"""
a3watch safety self-test (no external deps; run: python3 agent/tests/test_safety.py).

Asserts the two load-bearing guarantees at the code level:
  * NON-WAKING: the normal-mode command gate rejects anything that could spin a
    disk up, and no module contains a disk-waking / device-opening pattern
    outside the diagnostic module.
  * OBSERVE-ONLY: no module writes a power/spindown/governor setting.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
sys.path.insert(0, PKG)

from a3watch import util  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


print("== command gate ==")
# allowed, non-waking
check("hdparm -C permitted", util.run_cmd(["hdparm", "-C", "/dev/null"])[0] != 126)
# waking / mutating commands must be blocked in normal mode
for argv in (
    ["smartctl", "-a", "/dev/sda"],
    ["hdparm", "-S", "120", "/dev/sda"],   # set spindown timer (mutating + can be issued to disk)
    ["hdparm", "-y", "/dev/sda"],          # force standby
    ["hdparm", "-I", "/dev/sda"],          # identify — wakes the drive
    ["dd", "if=/dev/sda", "of=/dev/null"],
    ["cpupower", "frequency-set", "-g", "performance"],
    ["blktrace", "-d", "/dev/sda"],
):
    rc = util.run_cmd(argv)[0]
    check(f"blocked in normal mode: {' '.join(argv[:2])}", rc == 126,
          f"expected 126 (blocked), got {rc}")
# diagnostic bypass is explicit
check("diagnostic bypass requires allow_diagnostic",
      util.run_cmd(["turbostat"], allow_diagnostic=True)[0] != 126)

print("== source pattern scan (normal-mode modules) ==")
NORMAL_MODULES = ["util.py", "disks.py", "power.py", "procs.py", "sample.py",
                  "detect.py", "attribute.py", "api.py", "cli.py", "config.py", "db.py",
                  "sysmap.py"]
# Patterns target actual code (raw-device opens and quoted argv lists), not the
# prose in docstrings that describes what is forbidden.
FORBIDDEN = [
    (r"open\(\s*['\"]/dev/(sd|nvme|hd)", "opens a raw block device"),
    (r"['\"]smartctl['\"]\s*,\s*['\"]-a", "smartctl -a in argv (wakes disk)"),
    (r"['\"]hdparm['\"]\s*,\s*['\"]-[SByYt]", "hdparm spindown/standby/mutating flag in argv"),
    (r"['\"]hdparm['\"]\s*,\s*['\"]-I", "hdparm -I in argv (identify wakes disk)"),
    (r"scaling_governor['\"][^\n]*\bw['\"]", "writes cpufreq governor"),
    (r"['\"]dd['\"]\s*,\s*['\"]if=/dev", "dd on a device in argv"),
]
import glob  # noqa: E402
paths = [os.path.join(PKG, "a3watch", m) for m in NORMAL_MODULES]
# collector modules run every cycle => normal-mode; scan them too (diag.py is excluded).
paths += sorted(glob.glob(os.path.join(PKG, "a3watch", "collect", "*.py")))
for path in paths:
    mod = os.path.relpath(path, os.path.join(PKG, "a3watch"))
    with open(path) as fh:
        src = fh.read()
    for pat, why in FORBIDDEN:
        hits = [ln for ln in src.splitlines()
                if re.search(pat, ln) and not ln.strip().startswith("#")]
        check(f"{mod}: no pattern [{why}]", not hits, f"{hits[:1]}")

print("== hdparm wrapper only uses -C ==")
with open(os.path.join(PKG, "a3watch", "util.py")) as fh:
    usrc = fh.read()
m = re.search(r"def hdparm_power_state.*?return None", usrc, re.S)
check("hdparm_power_state body only references -C",
      m and '"-C"' in m.group(0) and not re.search(r'"-[SByYtI]"', m.group(0)))

print("== hwmon must never read the drivetemp chip (would wake standby HDDs) ==")
try:
    from a3watch.collect import safe as _safe  # noqa: E402
    chips = {r["chip"] for r in _safe.hwmon()}
    check("collect.safe.hwmon excludes 'drivetemp'", "drivetemp" not in chips, f"chips={chips}")
except Exception as e:
    check("collect.safe.hwmon runnable", False, str(e))

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} check(s): {', '.join(FAILS)}")
    sys.exit(1)
print("All safety checks passed.")
