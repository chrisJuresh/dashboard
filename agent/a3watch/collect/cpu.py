"""CPU inventory + frequency + C-state availability (cheap sysfs)."""
from __future__ import annotations
import os
from . import Collector, Metric, Ctx, register, safe
from .. import power

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
        # deepest core C-state the cores actually REACH, decoded from each state's
        # MWAIT hint — not its ACPI name. On this platform the names collapse to
        # C1_ACPI/C2_ACPI/C3_ACPI while the hints (0x21->C6, 0x60->C10) show the
        # cores go all the way to C10, so name-matching would wrongly say "no deep
        # C-states". "Deep available" is a CORE fact; package residency is gated
        # separately (see Power group PC2..PC10).
        info = power.core_cstate_info()
        if info.get("names"):
            out.append(Metric("cstates.exposed", text=" ".join(info["names"])))
            deepest = info.get("deepest") or "?"
            out.append(Metric("cstates.deepest", text=deepest))
            deep = bool(info.get("deep_available"))
            out.append(Metric("cstates.deep_available", num=1 if deep else 0,
                              text=(f"yes — cores reach {deepest}" if deep
                                    else f"no — cores stop at {deepest} (a BIOS/kernel setting)")))
        return out


register(Cpu())
