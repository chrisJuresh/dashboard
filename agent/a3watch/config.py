"""
a3watch.config — the editable configuration model.

Config lives at /etc/a3watch/config.toml (on NVMe). It is written by `detect`
with sane detected defaults, then a human reviews/edits it, then `install
--confirm` applies it. Reading uses stdlib tomllib; writing uses a small
emitter (stdlib has no TOML writer) that supports exactly the shapes we use.

The API bearer token is NOT stored here — it lives in <data_dir>/token with
0600 perms so the world-readable config never contains a secret.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, asdict
from typing import Optional

from . import util

DEFAULT_CONFIG_PATH = "/etc/a3watch/config.toml"
DEFAULT_DATA_DIR = "/var/lib/a3watch"


@dataclass
class DiskCfg:
    dev: str
    role: str = "unknown"          # system|pool|parity|backup|data|unknown
    model: str = ""
    serial: str = ""
    label: str = ""
    mount: str = ""
    fs: str = ""
    size_bytes: int = 0
    rotational: bool = True
    pool: str = ""
    maj_min: str = ""              # '8:0' — block device major:minor
    # protected=True => never issue any command (incl. non-waking hdparm -C);
    # state is inferred passively. `detect` sets this per drive based on the mode:
    # when use_hdparm_c is enabled it writes protected=False for rotational drives
    # so the chosen (non-waking) authoritative probing is actually active.
    protected: bool = True
    monitored: bool = True


@dataclass
class Config:
    data_dir: str = DEFAULT_DATA_DIR
    interval_s: int = 20
    raw_days: int = 14
    rollup_days: int = 365
    budget_gbp_year: float = 5.0
    electricity_gbp_per_kwh: float = 0.25
    # disk probing
    use_hdparm_c: bool = True
    # api
    api_bind: str = "127.0.0.1"
    api_port: int = 8787
    api_idle_exit_s: int = 120
    allow_origins: list[str] = field(default_factory=list)
    # tunnel (informational; user wires cloudflared themselves)
    tunnel_hostname: str = ""
    # unified same-origin deployment: serve the built SPA + gate with Cloudflare Access
    web_root: str = ""               # dir of built SPA assets; defaults to <data_dir>/web
    cf_access_team_domain: str = ""  # e.g. myteam.cloudflareaccess.com (enables Access JWT auth)
    cf_access_aud: str = ""          # the Access application's AUD tag
    # topology
    disks: list[DiskCfg] = field(default_factory=list)

    # ---- derived paths (all under data_dir → NVMe) --------------------
    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "a3watch.db")

    @property
    def token_path(self) -> str:
        return os.path.join(self.data_dir, "token")

    @property
    def diag_dir(self) -> str:
        return os.path.join(self.data_dir, "diag")

    @property
    def web_dir(self) -> str:
        return self.web_root or os.path.join(self.data_dir, "web")

    @property
    def cf_access_enabled(self) -> bool:
        return bool(self.cf_access_team_domain and self.cf_access_aud)

    def disk(self, dev: str) -> Optional[DiskCfg]:
        for d in self.disks:
            if d.dev == dev:
                return d
        return None


# ------------------------------------------------------------------ load ----
def load(path: str = DEFAULT_CONFIG_PATH) -> Config:
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)
    core = raw.get("core", {})
    probe = raw.get("disk_probe", {})
    api = raw.get("api", {})
    tunnel = raw.get("tunnel", {})
    access = raw.get("access", {})
    cfg = Config(
        data_dir=core.get("data_dir", DEFAULT_DATA_DIR),
        interval_s=int(core.get("interval_s", 20)),
        raw_days=int(core.get("raw_days", 14)),
        rollup_days=int(core.get("rollup_days", 365)),
        budget_gbp_year=float(core.get("budget_gbp_year", 5.0)),
        electricity_gbp_per_kwh=float(core.get("electricity_gbp_per_kwh", 0.25)),
        use_hdparm_c=bool(probe.get("use_hdparm_c", True)),
        api_bind=api.get("bind", "127.0.0.1"),
        api_port=int(api.get("port", 8787)),
        api_idle_exit_s=int(api.get("idle_exit_s", 120)),
        allow_origins=list(api.get("allow_origins", [])),
        tunnel_hostname=tunnel.get("hostname", ""),
        web_root=access.get("web_root", ""),
        cf_access_team_domain=access.get("team_domain", ""),
        cf_access_aud=access.get("aud", ""),
    )
    for d in raw.get("disk", []):
        cfg.disks.append(
            DiskCfg(
                dev=d["dev"],
                role=d.get("role", "unknown"),
                model=d.get("model", ""),
                serial=d.get("serial", ""),
                label=d.get("label", ""),
                mount=d.get("mount", ""),
                fs=d.get("fs", ""),
                size_bytes=int(d.get("size_bytes", 0)),
                rotational=bool(d.get("rotational", True)),
                pool=d.get("pool", ""),
                maj_min=d.get("maj_min", ""),
                protected=bool(d.get("protected", True)),
                monitored=bool(d.get("monitored", True)),
            )
        )
    return cfg


# ------------------------------------------------------------------ dump ----
def _toml_str(v: str) -> str:
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _toml_val(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_val(x) for x in v) + "]"
    return _toml_str(str(v))


def dumps(cfg: Config) -> str:
    lines: list[str] = []
    lines.append("# a3watch configuration — REVIEW AND EDIT, then run: a3watch install --confirm")
    lines.append("# All paths must resolve to the NVMe/SSD. Rotational data dirs are rejected.")
    lines.append("")
    lines.append("[core]")
    lines.append(f"data_dir = {_toml_str(cfg.data_dir)}")
    lines.append(f"interval_s = {cfg.interval_s}              # sampling cadence (seconds)")
    lines.append(f"raw_days = {cfg.raw_days}                  # keep per-cycle raw rows this long")
    lines.append(f"rollup_days = {cfg.rollup_days}            # keep hourly rollups + events this long")
    lines.append(f"budget_gbp_year = {cfg.budget_gbp_year}    # power-budget target shown on dashboard")
    lines.append(f"electricity_gbp_per_kwh = {cfg.electricity_gbp_per_kwh}")
    lines.append("")
    lines.append("[disk_probe]")
    lines.append(f"use_hdparm_c = {_toml_val(cfg.use_hdparm_c)}  # non-waking ATA CHECK POWER MODE for authoritative state")
    lines.append("")
    lines.append("[api]")
    lines.append(f"bind = {_toml_str(cfg.api_bind)}")
    lines.append(f"port = {cfg.api_port}")
    lines.append(f"idle_exit_s = {cfg.api_idle_exit_s}       # socket-activated API exits after idle")
    lines.append(f"allow_origins = {_toml_val(cfg.allow_origins)}  # CORS allow-list (your Vercel URL)")
    lines.append("")
    lines.append("[tunnel]")
    lines.append(f"hostname = {_toml_str(cfg.tunnel_hostname)}  # informational; you wire cloudflared")
    lines.append("")
    lines.append("[access]")
    lines.append("# Cloudflare Access SSO gate for the unified same-origin deployment. When set, the")
    lines.append("# agent serves the built dashboard AND verifies Cloudflare's signed login token, so")
    lines.append("# no bearer token is typed in the browser. Leave blank for token-only mode.")
    lines.append(f"team_domain = {_toml_str(cfg.cf_access_team_domain)}  # e.g. myteam.cloudflareaccess.com")
    lines.append(f"aud = {_toml_str(cfg.cf_access_aud)}                  # Access application AUD tag")
    lines.append(f"web_root = {_toml_str(cfg.web_root)}                  # blank => <data_dir>/web")
    lines.append("")
    lines.append("# ---- Detected storage topology. Adjust roles / protection as needed. ----")
    lines.append("# role: system|pool|parity|backup|data|unknown")
    lines.append("# protected=true means a3watch will NEVER issue any command to this drive")
    lines.append("#   (not even non-waking hdparm -C); its state is inferred passively.")
    lines.append(f"# NOTE: use_hdparm_c={_toml_val(cfg.use_hdparm_c)} above. When it is true, detected rotational")
    lines.append("#   drives are written with protected=FALSE below, i.e. authoritative non-waking")
    lines.append("#   hdparm -C probing is ENABLED for them. Set protected=true on any drive to")
    lines.append("#   exclude it completely. hdparm -C (ATA CHECK POWER MODE) does not spin a disk up.")
    for d in cfg.disks:
        lines.append("")
        lines.append("[[disk]]")
        lines.append(f"dev = {_toml_str(d.dev)}")
        lines.append(f"role = {_toml_str(d.role)}")
        lines.append(f"label = {_toml_str(d.label)}")
        lines.append(f"model = {_toml_str(d.model)}")
        lines.append(f"mount = {_toml_str(d.mount)}")
        lines.append(f"fs = {_toml_str(d.fs)}")
        lines.append(f"size_bytes = {d.size_bytes}")
        lines.append(f"rotational = {_toml_val(d.rotational)}")
        lines.append(f"pool = {_toml_str(d.pool)}")
        lines.append(f"maj_min = {_toml_str(d.maj_min)}")
        lines.append(f"protected = {_toml_val(d.protected)}")
        lines.append(f"monitored = {_toml_val(d.monitored)}")
    lines.append("")
    return "\n".join(lines)


def save(cfg: Config, path: str = DEFAULT_CONFIG_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(dumps(cfg))
    os.replace(tmp, path)


# -------------------------------------------------------------- validate ----
class ConfigError(Exception):
    pass


def validate_data_dir_nonrotational(data_dir: str) -> tuple[bool, str]:
    """Return (ok, message). The data dir MUST be backed by a non-rotational
    device so we never store our own data on the HDDs we are trying to keep
    asleep. Returns ok=True with a note if the backing device cannot be
    resolved (e.g. tmpfs during a dry run) so detection isn't blocked."""
    parent = data_dir
    while parent and not util.path_exists(parent):
        parent = os.path.dirname(parent)
    if not parent:
        parent = "/"
    dev = util.backing_block_device(parent)
    if dev is None:
        return (True, f"could not resolve backing device for {data_dir}; assuming OK")
    rot = util.is_rotational(dev)
    if rot is True:
        return (False, f"data_dir {data_dir} is backed by ROTATIONAL device {dev}; refusing")
    return (True, f"data_dir {data_dir} backed by non-rotational {dev} — OK")


def validate(cfg: Config) -> list[str]:
    errors: list[str] = []
    ok, msg = validate_data_dir_nonrotational(cfg.data_dir)
    if not ok:
        errors.append(msg)
    if cfg.interval_s < 5:
        errors.append("interval_s must be >= 5 (protect power budget / avoid wakeup storms)")
    if cfg.api_bind not in ("127.0.0.1", "::1", "localhost") and not cfg.allow_origins:
        errors.append("api bound to a non-local address without an allow_origins CORS list")
    return errors
