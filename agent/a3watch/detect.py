"""
a3watch.detect — non-waking topology + capability detection.

Everything here reads sysfs / /proc / config files (and, best-effort, the
docker socket and systemctl metadata). Nothing opens a block device or issues
a disk command, so detection can never spin a drive up.
"""

from __future__ import annotations

import os

from . import power, util
from .config import Config, DiskCfg


# --------------------------------------------------- block enumeration ------
def _parent_disk(part: str) -> str:
    try:
        real = os.path.realpath(f"/sys/class/block/{part}")
        if util.path_exists(os.path.join(real, "partition")):
            return os.path.basename(os.path.dirname(real))
    except OSError:
        pass
    return part


def _device_labels() -> dict[str, str]:
    """partition-name -> filesystem label, from /dev/disk/by-label symlinks."""
    out: dict[str, str] = {}
    base = "/dev/disk/by-label"
    for lbl in util.list_dir(base):
        try:
            tgt = os.path.basename(os.path.realpath(os.path.join(base, lbl)))
            out[tgt] = lbl
        except OSError:
            continue
    return out


def _mounts() -> dict[str, tuple[str, str]]:
    """partition-name -> (mountpoint, fstype), from /proc/mounts (real devs)."""
    out: dict[str, tuple[str, str]] = {}
    for line in util.read_text("/proc/mounts").splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        src, target, fstype = parts[0], parts[1], parts[2]
        if not src.startswith("/dev/"):
            continue
        name = os.path.basename(os.path.realpath(src))
        out.setdefault(name, (target.replace("\\040", " "), fstype))
    return out


def _dev_model(dev: str) -> str:
    return util.read_first_line(f"/sys/block/{dev}/device/model").strip()


def _dev_serial(dev: str) -> str:
    for p in (
        f"/sys/block/{dev}/device/serial",
        f"/sys/block/{dev}/device/wwid",
        f"/sys/block/{dev}/device/vpd_pg80",
    ):
        s = util.read_first_line(p).strip()
        if s:
            return s
    return ""


def _dev_size_bytes(dev: str) -> int:
    sectors = util.read_int(f"/sys/block/{dev}/size", 0) or 0
    return sectors * 512


# ------------------------------------------------------ pool / raid ---------
def _mergerfs_pools() -> dict[str, dict]:
    """Parse fstab for fuse.mergerfs lines. Returns {fsname: {branches:[mnt],
    target}}. Branch mountpoints are used to tag the underlying disks."""
    pools: dict[str, dict] = {}
    for line in util.read_text("/etc/fstab").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "fuse.mergerfs" not in line:
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        spec, target, opts = parts[0], parts[1], parts[3]
        branches = []
        for b in spec.split(":"):
            b = b.split("=")[0]  # strip '=NC' etc.
            # branch like /mnt/bay3/media -> mountpoint /mnt/bay3
            branches.append(b)
        fsname = target
        for opt in opts.split(","):
            if opt.startswith("fsname="):
                fsname = opt.split("=", 1)[1]
        pools[fsname] = {"branches": branches, "target": target}
    return pools


def _snapraid() -> dict:
    """Parse /etc/snapraid.conf -> {parity_mounts:set, data_mounts:set}."""
    parity, data = set(), set()
    for line in util.read_text("/etc/snapraid.conf").splitlines():
        line = line.strip()
        if line.startswith("parity") or line.startswith("2-parity") or line.startswith("z-parity"):
            fields = line.split()
            if len(fields) >= 2:
                parity.add(fields[1].split(",")[0])
        elif line.startswith("data"):
            fields = line.split()
            if len(fields) >= 3:
                data.add(fields[2])
    return {"parity": parity, "data": data}


def _mount_to_dev(mounts: dict[str, tuple[str, str]]) -> dict[str, str]:
    """mountpoint -> whole-disk dev."""
    out: dict[str, str] = {}
    for part, (mnt, _fs) in mounts.items():
        out[mnt] = _parent_disk(part)
    return out


def _path_owner_mount(path: str, mount_to_dev: dict[str, str]) -> str:
    """Longest mountpoint prefix of path -> whole-disk dev."""
    best = ""
    for mnt in mount_to_dev:
        if path == mnt or path.startswith(mnt.rstrip("/") + "/"):
            if len(mnt) > len(best):
                best = mnt
    return mount_to_dev.get(best, "")


# --------------------------------------------------------- capabilities -----
def _timers() -> list[str]:
    rc, out, _ = util.run_cmd(["systemctl", "list-timers", "--all", "--no-legend"], timeout=5.0)
    units: list[str] = []
    if rc == 0:
        for line in out.splitlines():
            toks = line.split()
            # last two columns are UNIT ACTIVATES
            if len(toks) >= 2:
                units.append(toks[-2])
    return units


def _docker_containers() -> list[dict]:
    rc, out, _ = util.run_cmd(
        ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Image}}\t{{.Status}}"],
        timeout=5.0,
        allow_diagnostic=True,
    )
    res: list[dict] = []
    if rc == 0:
        for line in out.splitlines():
            f = line.split("\t")
            if len(f) >= 3:
                res.append({"name": f[0], "image": f[1], "status": f[2]})
    return res


def _capabilities() -> dict:
    tools = {}
    for t in (
        "hdparm", "smartctl", "turbostat", "powertop", "mdadm", "snapraid",
        "docker", "bpftrace", "biosnoop", "biosnoop-bpfcc", "blktrace", "iostat",
    ):
        tools[t] = util.have_cmd(t)
    return {
        "tools": tools,
        "rapl_domains": sorted(power.read_rapl_energy().keys()),
        "pkg_cstates_msr": power.pkg_cstates_available(),
        "cpuidle_driver": util.read_first_line("/sys/devices/system/cpu/cpuidle/current_driver"),
        "governor": util.read_first_line(
            "/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"
        ),
        "timers": _timers(),
    }


# -------------------------------------------------------------- detect ------
def detect() -> dict:
    mounts = _mounts()
    labels = _device_labels()
    mount_to_dev = _mount_to_dev(mounts)
    pools = _mergerfs_pools()
    snap = _snapraid()
    # resolve the device backing '/'
    root_backing = util.backing_block_device("/")

    disks: list[dict] = []
    for dev in util.block_devices():
        rot = util.is_rotational(dev)
        # find this disk's primary partition mount/label
        mount, fs, label = "", "", ""
        for part, (mnt, fstype) in mounts.items():
            if _parent_disk(part) == dev:
                mount, fs = mnt, fstype
                label = labels.get(part, label)
                break
        if not label:
            for part, lbl in labels.items():
                if _parent_disk(part) == dev:
                    label = lbl
                    break
        # pool membership
        pool = ""
        for fsname, pinfo in pools.items():
            for branch in pinfo["branches"]:
                if _path_owner_mount(branch, mount_to_dev) == dev:
                    pool = fsname
        disks.append(
            {
                "dev": dev,
                "rotational": bool(rot),
                "model": _dev_model(dev),
                "serial": _dev_serial(dev),
                "size_bytes": _dev_size_bytes(dev),
                "mount": mount,
                "fs": fs,
                "label": label,
                "maj_min": util.dev_maj_min(dev) or "",
                "pool": pool,
                "is_root": dev == root_backing,
            }
        )

    return {
        "disks": disks,
        "pools": pools,
        "snapraid": snap,
        "mount_to_dev": mount_to_dev,
        "containers": _docker_containers(),
        "capabilities": _capabilities(),
        "root_dev": root_backing,
    }


# --------------------------------------------------------- build config -----
def _classify_role(d: dict, snap: dict, mount_to_dev: dict[str, str]) -> str:
    if not d["rotational"]:
        return "system" if d["is_root"] else "ssd"
    label = (d["label"] or "").lower()
    # snapraid parity/data by mountpoint prefix
    parity_devs = {mount_to_dev.get(_longest_mount(p, mount_to_dev), "") for p in snap["parity"]}
    data_devs = {mount_to_dev.get(_longest_mount(p, mount_to_dev), "") for p in snap["data"]}
    if d["dev"] in parity_devs or label.endswith("par") or "parity" in label:
        return "parity"
    if d["dev"] in data_devs:
        return "data" if not d["pool"] else "pool"
    if "bak" in label or "backup" in label:
        return "backup"
    if d["pool"]:
        return "pool"
    return "unknown"


def _longest_mount(path: str, mount_to_dev: dict[str, str]) -> str:
    best = ""
    for mnt in mount_to_dev:
        if path == mnt or path.startswith(mnt.rstrip("/") + "/"):
            if len(mnt) > len(best):
                best = mnt
    return best


def build_config(detection: dict, existing: Config | None, use_hdparm_c: bool = True) -> Config:
    cfg = existing or Config()
    cfg.use_hdparm_c = use_hdparm_c
    snap = detection["snapraid"]
    mount_to_dev = detection["mount_to_dev"]

    prev = {d.dev: d for d in cfg.disks} if existing else {}
    new_disks: list[DiskCfg] = []
    for d in detection["disks"]:
        role = _classify_role(d, snap, mount_to_dev)
        # preserve human edits from an existing config where present
        old = prev.get(d["dev"])
        protected_default = (not use_hdparm_c) if d["rotational"] else False
        new_disks.append(
            DiskCfg(
                dev=d["dev"],
                role=old.role if old else role,
                model=d["model"],
                serial=d["serial"],
                label=d["label"],
                mount=d["mount"],
                fs=d["fs"],
                size_bytes=d["size_bytes"],
                rotational=d["rotational"],
                pool=d["pool"],
                maj_min=d["maj_min"],
                protected=old.protected if old else protected_default,
                monitored=old.monitored if old else True,
            )
        )
    cfg.disks = new_disks
    return cfg
