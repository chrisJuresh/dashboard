"""CPU inventory + frequency + C-state availability (cheap sysfs)."""
from __future__ import annotations
import os
from . import Collector, Metric, Ctx, register, safe

_CPU = "/sys/devices/system/cpu"


class Cpu(Collector):
    name = "cpu"
    group = "CPU"

    def collect(self, ctx: Ctx) -> list[Metric]:
        out: list[Metric] = []
        cpus = [c for c in safe.read_text("/proc/cpuinfo").splitlines() if c.startswith("model name")]
        if cpus:
            out.append(Metric("model", text=cpus[0].split(":", 1)[1].strip()))
        ncpu = sum(1 for d in os.listdir(_CPU) if d.startswith("cpu") and d[3:].isdigit())
        out.append(Metric("logical_cpus", num=ncpu))
        # frequencies (mean/min/max across online cores)
        freqs = []
        for i in range(ncpu):
            f = safe.read_int(f"{_CPU}/cpu{i}/cpufreq/scaling_cur_freq")
            if f:
                freqs.append(f / 1000.0)  # MHz
        if freqs:
            out.append(Metric("freq.avg", num=round(sum(freqs) / len(freqs), 0), unit="MHz", series=True))
            out.append(Metric("freq.max_seen", num=round(max(freqs), 0), unit="MHz"))
        gmin = safe.read_int(f"{_CPU}/cpu0/cpufreq/cpuinfo_min_freq")
        gmax = safe.read_int(f"{_CPU}/cpu0/cpufreq/cpuinfo_max_freq")
        if gmin:
            out.append(Metric("freq.hw_min", num=gmin / 1000.0, unit="MHz"))
        if gmax:
            out.append(Metric("freq.hw_max", num=gmax / 1000.0, unit="MHz"))
        out.append(Metric("governor", text=safe.read_text(f"{_CPU}/cpu0/cpufreq/scaling_governor").strip() or "?"))
        out.append(Metric("scaling_driver", text=safe.read_text(f"{_CPU}/cpu0/cpufreq/scaling_driver").strip() or "?"))
        out.append(Metric("idle_driver", text=safe.read_text(f"{_CPU}/cpuidle/current_driver").strip() or "?"))
        # deepest exposed core C-state + whether deep idle is even available
        states = []
        idle0 = f"{_CPU}/cpu0/cpuidle"
        if os.path.isdir(idle0):
            for st in sorted(os.listdir(idle0)):
                if st.startswith("state"):
                    nm = safe.read_text(f"{idle0}/{st}/name").strip()
                    if nm:
                        states.append(nm)
        if states:
            out.append(Metric("cstates.exposed", text=" ".join(states)))
            out.append(Metric("cstates.deepest", text=states[-1]))
            deep = any(any(tag in s.upper() for tag in ("C6", "C7", "C8", "C9", "C10")) for s in states)
            out.append(Metric("cstates.deep_available", num=1 if deep else 0,
                              text="yes" if deep else "no (only up to " + states[-1] + " — a BIOS/kernel setting)"))
        return out


register(Cpu())
