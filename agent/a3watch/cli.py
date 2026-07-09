"""
a3watch.cli — command surface.

  detect     read the system (non-waking), write an editable config, print a summary
  install    (needs --confirm) validate config, create data dir + systemd units, enable timer/socket
  sample     run one sampling cycle (the timer target)
  serve      run the read-only API (the socket service target)
  status     print a live, non-waking snapshot + latest recorded metrics
  diag       run a time-boxed diagnostic session (may add overhead; smart may wake a disk)
  uninstall  (needs --confirm) stop/disable units and remove them
  version

The two-step detect → (review config) → install --confirm flow IS the mandatory
manual-adjust gate: no systemd unit is created and no package installed until the
user has reviewed /etc/a3watch/config.toml and explicitly confirms.
"""

from __future__ import annotations

import argparse
import os
import secrets
import subprocess
import sys
import time

from . import __version__, config as cfgmod, detect as detectmod, disks


BIN_PATH = "/usr/local/bin/a3watch"
PKG_ROOT = "/opt/a3watch"


# ----------------------------------------------------- systemd templates ----
def _units(cfg: cfgmod.Config) -> dict[str, str]:
    common = (
        "NoNewPrivileges=yes\n"
        "ProtectSystem=strict\n"
        "ProtectHome=yes\n"
        "PrivateTmp=yes\n"
        f"ReadWritePaths={cfg.data_dir}\n"
        "ProtectKernelLogs=yes\n"
        "RestrictSUIDSGID=yes\n"
        "LockPersonality=yes\n"
    )
    return {
        "a3watch-sample.service": (
            "[Unit]\n"
            "Description=a3watch sampling cycle (measure disks/power/C-states, non-waking)\n"
            "After=multi-user.target\n\n"
            "[Service]\n"
            "Type=oneshot\n"
            f"ExecStart={BIN_PATH} sample\n"
            # sampler needs /dev/cpu/*/msr + powercap + hdparm ioctl → keep devices visible
            + common
        ),
        "a3watch-sample.timer": (
            "[Unit]\n"
            "Description=a3watch sampling cadence\n\n"
            "[Timer]\n"
            "OnBootSec=45s\n"
            f"OnUnitActiveSec={cfg.interval_s}s\n"
            "AccuracySec=2s\n"
            "Persistent=false\n\n"
            "[Install]\n"
            "WantedBy=timers.target\n"
        ),
        "a3watch-api.socket": (
            "[Unit]\n"
            "Description=a3watch read-only API socket\n\n"
            "[Socket]\n"
            f"ListenStream={cfg.api_bind}:{cfg.api_port}\n"
            "Accept=no\n\n"
            "[Install]\n"
            "WantedBy=sockets.target\n"
        ),
        "a3watch-api.service": (
            "[Unit]\n"
            "Description=a3watch read-only API (socket-activated, idle-exits)\n"
            "Requires=a3watch-api.socket\n"
            "After=a3watch-api.socket\n\n"
            "[Service]\n"
            f"ExecStart={BIN_PATH} serve\n"
            # NOTE: no PrivateDevices — the API launches diagnostic tracers
            # (turbostat/blktrace/biosnoop) which need /dev/cpu/*/msr and block
            # device nodes. The API is otherwise hardened + auth-gated + local.
            + common
        ),
    }


# ------------------------------------------------------------ commands ------
def cmd_detect(args) -> int:
    existing = None
    if os.path.exists(args.config):
        try:
            existing = cfgmod.load(args.config)
        except Exception as e:
            print(f"warning: could not load existing config ({e}); regenerating", file=sys.stderr)
    detection = detectmod.detect()
    cfg = detectmod.build_config(detection, existing, use_hdparm_c=not args.no_hdparm)
    if args.data_dir:
        cfg.data_dir = args.data_dir
    _print_detection(cfg, detection)
    errors = cfgmod.validate(cfg)
    ok, msg = cfgmod.validate_data_dir_nonrotational(cfg.data_dir)
    print(f"\ndata dir check: {msg}")
    if errors:
        print("\nvalidation issues:")
        for e in errors:
            print(f"  - {e}")
    if args.dry_run:
        print("\n[dry-run] not writing config.")
        return 0 if not errors else 1
    cfgmod.save(cfg, args.config)
    print(f"\nWrote {args.config}")
    print("Review/edit it (roles, protected drives, interval), then run:")
    print("  sudo a3watch install --confirm")
    return 0


def _print_detection(cfg: cfgmod.Config, detection: dict) -> None:
    caps = detection["capabilities"]
    print("=== a3watch detected topology (non-waking) ===")
    print(f"{'dev':10} {'role':8} {'rot':4} {'size':8} {'mount':14} {'label':16} model")
    for d in cfg.disks:
        gb = d.size_bytes / 1e12
        size = f"{gb:.1f}T" if gb >= 1 else f"{d.size_bytes/1e9:.0f}G"
        print(f"{d.dev:10} {d.role:8} {'yes' if d.rotational else 'no ':4} "
              f"{size:8} {d.mount:14} {d.label:16} {d.model}")
    print(f"\nRAPL domains: {', '.join(caps['rapl_domains']) or 'none (need root)'}")
    print(f"Package C-states via MSR: {'yes' if caps['pkg_cstates_msr'] else 'no (diagnostic turbostat only)'}")
    print(f"CPU idle driver: {caps['cpuidle_driver']}   governor: {caps['governor']}")
    tools = caps["tools"]
    print("Tools: " + ", ".join(f"{k}={'y' if v else 'n'}" for k, v in tools.items()))
    if detection["containers"]:
        print(f"Docker containers: {len(detection['containers'])} "
              f"({', '.join(c['name'] for c in detection['containers'][:8])})")
    print(f"hdparm -C authoritative state: {'ENABLED' if cfg.use_hdparm_c else 'disabled'} "
          f"(non-waking; per-drive 'protected' overrides)")


def cmd_install(args) -> int:
    cfg = cfgmod.load(args.config)
    errors = cfgmod.validate(cfg)
    ok, msg = cfgmod.validate_data_dir_nonrotational(cfg.data_dir)
    if not ok:
        print(f"REFUSING: {msg}", file=sys.stderr)
        return 2
    if errors:
        print("Fix these config issues first:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 2

    units = _units(cfg)
    print("a3watch install plan:")
    print(f"  data dir : {cfg.data_dir}  ({msg})")
    print(f"  interval : {cfg.interval_s}s")
    print(f"  API      : {cfg.api_bind}:{cfg.api_port}")
    print(f"  units    : {', '.join(units)}")
    print(f"  diag pkgs: bpfcc-tools, bpftrace, blktrace, auditd (installed, dormant until diagnostic mode)")
    if not args.confirm:
        print("\nRe-run with --confirm to apply. Nothing changed.")
        return 0

    if os.geteuid() != 0:
        print("install --confirm must run as root (sudo).", file=sys.stderr)
        return 2

    os.makedirs(cfg.data_dir, exist_ok=True)
    os.chmod(cfg.data_dir, 0o750)
    os.makedirs(cfg.diag_dir, exist_ok=True)
    if not os.path.exists(cfg.token_path):
        with open(cfg.token_path, "w") as fh:
            fh.write(secrets.token_urlsafe(32))
        os.chmod(cfg.token_path, 0o600)
        print(f"  generated API token at {cfg.token_path}")

    if args.with_diag:
        _apt_install(["bpfcc-tools", "bpftrace", "blktrace", "auditd"])

    for name, body in units.items():
        path = f"/etc/systemd/system/{name}"
        with open(path, "w") as fh:
            fh.write(body)
        print(f"  wrote {path}")
    subprocess.run(["systemctl", "daemon-reload"], check=False)
    subprocess.run(["systemctl", "enable", "--now", "a3watch-sample.timer"], check=False)
    # Restart (not just enable --now) the socket so a changed bind/port from a
    # re-install actually takes effect; stop the service first to release the old fd.
    subprocess.run(["systemctl", "stop", "a3watch-api.service"], check=False)
    subprocess.run(["systemctl", "enable", "a3watch-api.socket"], check=False)
    subprocess.run(["systemctl", "restart", "a3watch-api.socket"], check=False)
    print("\nEnabled a3watch-sample.timer and a3watch-api.socket.")
    _print_tunnel_help(cfg)
    print("\nUninstall any time with:  sudo a3watch uninstall --confirm")
    return 0


def _apt_install(pkgs: list[str]) -> None:
    print(f"  apt-get install -y {' '.join(pkgs)} …")
    env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
    subprocess.run(["apt-get", "update", "-qq"], check=False, env=env)
    rc = subprocess.run(["apt-get", "install", "-y", "-qq", *pkgs], check=False, env=env)
    if rc.returncode != 0:
        print("  (diag package install failed — diagnostic mode will report which tools are missing)")


def _print_tunnel_help(cfg: cfgmod.Config) -> None:
    try:
        token = open(cfg.token_path).read().strip()
    except OSError:
        token = "<see " + cfg.token_path + ">"
    host = cfg.tunnel_hostname or "a3watch.example.com"
    print("\n--- Cloudflare tunnel (apply to YOUR cloudflared config; not done automatically) ---")
    print("Add an ingress rule mapping a hostname to the local API, e.g.:")
    print(f"  - hostname: {host}")
    print(f"    service: http://{cfg.api_bind}:{cfg.api_port}")
    print("Then in the dashboard's connect screen enter:")
    print(f"  API base URL : https://{host}")
    print(f"  Bearer token : {token}")
    print("Also add your Vercel origin to [api].allow_origins in the config (CORS).")


def cmd_sample(args) -> int:
    from . import sample
    cfg = cfgmod.load(args.config)
    s = sample.run_once(cfg)
    print(f"sample ts={s['ts']:.0f} reset={s['reset']} events={s.get('events',0)} "
          f"power_events={s.get('power_events',0)} wall={s.get('wall_ms','?')}ms "
          f"cpu={s.get('cpu_ms','?')}ms db={s.get('db_bytes',0)}B")
    return 0


def cmd_serve(args) -> int:
    from . import api
    cfg = cfgmod.load(args.config)
    api.serve(cfg)
    return 0


def cmd_status(args) -> int:
    cfg = cfgmod.load(args.config)
    print("=== a3watch live status (non-waking) ===")
    for d in cfg.disks:
        if not d.monitored:
            continue
        state = disks.power_state(cfg, d.dev) if d.rotational else "n/a (ssd)"
        tag = "" if not d.rotational else ("  [asleep]" if state in ("standby", "sleeping") else "  [AWAKE]")
        print(f"  {d.dev:10} {d.role:8} {state:9}{tag}  {d.mount}  {d.label}")
    # latest recorded metrics if the DB exists
    if os.path.exists(cfg.db_path):
        import sqlite3
        try:
            c = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
            row = c.execute("SELECT watts FROM cpu_power WHERE domain='package' ORDER BY ts DESC LIMIT 1").fetchone()
            ov = c.execute("SELECT avg_watts, gbp_year, db_bytes FROM overhead ORDER BY ts DESC LIMIT 1").fetchone()
            n = c.execute("SELECT COUNT(*) FROM sample").fetchone()[0]
            if row:
                print(f"\n  package power: {row[0]:.2f} W (last sample)")
            if ov:
                print(f"  a3watch overhead: ~{ov[0]*1000:.1f} mW  →  £{ov[1]:.2f}/yr  "
                      f"(budget £{cfg.budget_gbp_year:.0f})   db={ov[2]/1e6:.1f} MB   samples={n}")
            c.close()
        except sqlite3.Error as e:
            print(f"  (db read error: {e})")
    else:
        print("\n  No database yet — is a3watch-sample.timer running? (sudo a3watch install --confirm)")
    return 0


def cmd_diag(args) -> int:
    from . import diag
    cfg = cfgmod.load(args.config)
    try:
        sid = diag.start(cfg, args.tool, args.seconds, args.dev, args.confirm_wake)
    except diag.DiagError as e:
        print(f"cannot start: {e}", file=sys.stderr)
        return 1
    print(f"diagnostic session {sid} ({args.tool}, {args.seconds}s) started…")
    if args.wait:
        deadline = time.time() + args.seconds + 6
        while time.time() < deadline:
            time.sleep(1)
        res = diag.result(cfg, sid)
        print("\n".join(res["lines"]))
        print(f"\n[{res['summary']}]")
    else:
        print(f"read results: a3watch ... (or GET /api/diag/result/{sid})")
    return 0


def cmd_uninstall(args) -> int:
    if not args.confirm:
        print("Re-run with --confirm to stop/disable and remove a3watch units.")
        return 0
    if os.geteuid() != 0:
        print("uninstall --confirm must run as root (sudo).", file=sys.stderr)
        return 2
    for unit in ("a3watch-sample.timer", "a3watch-api.socket", "a3watch-api.service"):
        subprocess.run(["systemctl", "disable", "--now", unit], check=False)
    for name in ("a3watch-sample.service", "a3watch-sample.timer",
                 "a3watch-api.socket", "a3watch-api.service"):
        p = f"/etc/systemd/system/{name}"
        if os.path.exists(p):
            os.remove(p)
            print(f"  removed {p}")
    subprocess.run(["systemctl", "daemon-reload"], check=False)
    cfg = cfgmod.load(args.config) if os.path.exists(args.config) else None
    if args.purge and cfg:
        import shutil
        shutil.rmtree(cfg.data_dir, ignore_errors=True)
        print(f"  purged {cfg.data_dir}")
    print("a3watch units removed. (config + data left in place unless --purge)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="a3watch", description="low-power storage/power observability")
    p.add_argument("--config", default=cfgmod.DEFAULT_CONFIG_PATH)
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("detect", help="detect topology and write an editable config")
    d.add_argument("--data-dir")
    d.add_argument("--no-hdparm", action="store_true", help="disable non-waking hdparm -C probing")
    d.add_argument("--dry-run", action="store_true")
    d.set_defaults(func=cmd_detect)

    i = sub.add_parser("install", help="install systemd units (needs --confirm)")
    i.add_argument("--confirm", action="store_true")
    i.add_argument("--with-diag", action="store_true", default=True,
                   help="apt-install diagnostic tracing tools (default on)")
    i.set_defaults(func=cmd_install)

    sub.add_parser("sample", help="run one sampling cycle").set_defaults(func=cmd_sample)
    sub.add_parser("serve", help="run the read-only API").set_defaults(func=cmd_serve)
    sub.add_parser("status", help="print a live non-waking snapshot").set_defaults(func=cmd_status)

    dg = sub.add_parser("diag", help="run a time-boxed diagnostic session")
    dg.add_argument("tool", choices=["audit", "biosnoop", "ext4slower", "bpftrace_bio",
                                     "blktrace", "turbostat", "powertop", "smart"])
    dg.add_argument("--seconds", type=int, default=15)
    dg.add_argument("--dev")
    dg.add_argument("--confirm-wake", dest="confirm_wake", action="store_true",
                    help="required for tools that can spin up a disk (smart)")
    dg.add_argument("--wait", action="store_true", help="wait and print results")
    dg.set_defaults(func=cmd_diag)

    u = sub.add_parser("uninstall", help="remove units (needs --confirm)")
    u.add_argument("--confirm", action="store_true")
    u.add_argument("--purge", action="store_true", help="also delete the data dir")
    u.set_defaults(func=cmd_uninstall)

    sub.add_parser("version").set_defaults(func=lambda a: (print(__version__), 0)[1])
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
