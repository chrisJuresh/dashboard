"""
a3watch.sysmap — a read-only, non-waking map of how the box is wired together:
docker, the cloudflared tunnel, Cloudflare Access, git + deploy, systemd
units/timers, domains, storage, and a ~/ file-tree inventory.

Guarantees (same spirit as the rest of a3watch):
  * READ-ONLY: reads files + `docker`/`git`/`systemctl` metadata; changes nothing.
  * NON-WAKING: touches no block device. The ~/ walk stays on the home
    filesystem (NVMe) and never crosses into another mount (so it can't descend
    into a spun-down HDD pool) and never follows symlinks.
  * SECRETS REDACTED: values that look like tokens/keys/passwords are replaced
    with a "[redacted]" marker; secret files are reported as path+mode+owner
    only, never contents. Nothing here ever emits a credential.

Assembled by build_map(cfg) into a single JSON-able dict the API serves.
"""
from __future__ import annotations

import json
import os
import pwd
import re
import stat

from . import util
from .config import Config

# ---- secret redaction ------------------------------------------------------
# keys whose VALUE must never be shown (env vars, config keys, json fields)
_SECRET_KEY = re.compile(
    r"(token|secret|password|passwd|api[_-]?key|access[_-]?key|private[_-]?key"
    r"|credential|_key$|^key$|auth|bearer|cookie|salt|seed)", re.I)
# a long opaque blob sitting in a value (JWTs, hex/base64 keys, cf tokens)
_SECRET_VALUE = re.compile(r"[A-Za-z0-9_\-]{28,}")
_REDACTED = "[redacted]"


def _redact_value(key: str, value: str) -> str:
    if not isinstance(value, str):
        return value
    if _SECRET_KEY.search(key or ""):
        return _REDACTED if value else value
    # bare long opaque token as a whole value
    v = value.strip()
    if _SECRET_VALUE.fullmatch(v):
        return _REDACTED
    return value


def _redact_text(text: str) -> str:
    """Redact `KEY=longblob` / `--token blob` style secrets inside a free string
    (e.g. a container command line)."""
    text = re.sub(r'(?i)(token|secret|password|api[_-]?key|auth|bearer)[=: ]+\S+',
                  r'\1=' + _REDACTED, text)
    return text


# ---- git -------------------------------------------------------------------
def _git(repo: str, *args: str):
    # read-only git metadata; allow_diagnostic bypasses the (hardware-focused)
    # command gate the same way detect.py reads docker metadata. sysmap only ever
    # issues read subcommands (remote/branch/log/status).
    rc, out, _ = util.run_cmd(["git", "-C", repo, *args], timeout=5.0, allow_diagnostic=True)
    return out.strip() if rc == 0 else ""


def _find_git_repos(root: str) -> list[str]:
    repos: list[str] = []
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return repos
    if os.path.isdir(os.path.join(root, ".git")):
        repos.append(root)
    for e in entries:
        p = os.path.join(root, e)
        if os.path.isdir(os.path.join(p, ".git")):
            repos.append(p)
    return repos


def git_map(home: str) -> list[dict]:
    out: list[dict] = []
    for repo in _find_git_repos(home):
        remote = _redact_text(_git(repo, "remote", "-v").splitlines()[0]) if _git(repo, "remote") else ""
        branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
        upstream = _git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        last = _git(repo, "log", "-1", "--format=%h %s (%cr)")
        dirty = _git(repo, "status", "--porcelain")
        wf_dir = os.path.join(repo, ".github", "workflows")
        workflows = sorted(util.list_dir(wf_dir)) if os.path.isdir(wf_dir) else []
        out.append({
            "path": repo,
            "remote": remote,
            "branch": branch,
            "upstream": upstream,
            "last_commit": last,
            "dirty_files": len([l for l in dirty.splitlines() if l.strip()]),
            "ci_workflows": workflows,
        })
    return out


# ---- docker ----------------------------------------------------------------
def _docker(*args: str, timeout: float = 6.0):
    rc, out, _ = util.run_cmd(["docker", *args], timeout=timeout, allow_diagnostic=True)
    return out if rc == 0 else ""


def docker_map(home: str) -> dict:
    ids = [l for l in _docker("ps", "-aq").splitlines() if l.strip()]
    containers: list[dict] = []
    for cid in ids:
        raw = _docker("inspect", cid)
        try:
            info = json.loads(raw)[0]
        except (ValueError, IndexError, KeyError):
            continue
        name = info.get("Name", "").lstrip("/")
        cfg = info.get("Config", {}) or {}
        netset = (info.get("NetworkSettings", {}) or {}).get("Networks", {}) or {}
        state = info.get("State", {}) or {}
        ports = []
        for cport, binds in (netset and (info.get("NetworkSettings", {}) or {}).get("Ports", {}) or {}).items():
            for b in (binds or []):
                ports.append(f"{b.get('HostIp','')}:{b.get('HostPort','')}->{cport}")
        mounts = [f"{m.get('Source','')}:{m.get('Destination','')}"
                  for m in (info.get("Mounts", []) or [])]
        containers.append({
            "name": name,
            "image": cfg.get("Image", ""),
            "state": state.get("Status", ""),
            "restarts": state.get("RestartCount", 0),
            "restart_policy": (info.get("HostConfig", {}) or {}).get("RestartPolicy", {}).get("Name", ""),
            "networks": sorted(netset.keys()),
            "ports": ports,
            "mounts": mounts,
            # command may carry a tunnel token etc. — redact before exposing
            "cmd": _redact_text(" ".join(cfg.get("Cmd") or [])),
        })
    containers.sort(key=lambda c: c["name"])
    networks = []
    for line in _docker("network", "ls", "--format", "{{.Name}}\t{{.Driver}}").splitlines():
        p = line.split("\t")
        if p and p[0]:
            networks.append({"name": p[0], "driver": p[1] if len(p) > 1 else ""})
    # compose files anywhere under home (shallow)
    compose = []
    for repo, _dirs, files in _walk_bounded(home, max_entries=4000, want_files=True):
        for f in files:
            if f in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
                compose.append(os.path.join(repo, f))
    return {"containers": containers, "networks": networks, "compose_files": sorted(set(compose))}


# ---- systemd ---------------------------------------------------------------
_UNIT_DIRS = ("/etc/systemd/system", "/run/systemd/system", "/usr/lib/systemd/system")


def _unit_path(name: str):
    for d in _UNIT_DIRS:
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return None


def _ini(text: str, key: str):
    return [m.group(1).strip() for m in
            re.finditer(rf"(?im)^\s*{re.escape(key)}\s*=\s*(.+?)\s*$", text)]


def systemd_map() -> dict:
    import glob
    units: list[dict] = []
    seen = set()
    for path in sorted(sum((glob.glob(os.path.join(d, "a3*.service")) +
                            glob.glob(os.path.join(d, "a3*.timer")) for d in _UNIT_DIRS), [])):
        name = os.path.basename(path)
        if name in seen:
            continue
        seen.add(name)
        txt = util.read_text(path)
        rc, active, _ = util.run_cmd(["systemctl", "is-active", name], timeout=4.0)
        rc2, enabled, _ = util.run_cmd(["systemctl", "is-enabled", name], timeout=4.0)
        entry = {
            "unit": name,
            "description": (_ini(txt, "Description") or [""])[0],
            "active": active.strip(),
            "enabled": enabled.strip(),
        }
        if name.endswith(".timer"):
            entry["schedule"] = ", ".join(_ini(txt, "OnCalendar") + _ini(txt, "OnUnitActiveSec")
                                          + _ini(txt, "OnBootSec"))
        else:
            entry["exec"] = _redact_text("; ".join(_ini(txt, "ExecStart")))
        units.append(entry)
    return {"units": units}


# ---- tunnel / access / domains --------------------------------------------
def tunnel_map(cfg: Config, containers: list[dict]) -> dict:
    cf = next((c for c in containers if "cloudflared" in c["name"] or "cloudflared" in c["image"]), None)
    return {
        "running": bool(cf and cf["state"] == "running"),
        "container": cf["name"] if cf else "",
        "image": cf["image"] if cf else "",
        "note": "token-managed tunnel — full ingress rules live in the Cloudflare "
                "dashboard (see the one-off cloud snapshot). Locally we can only see "
                "that cloudflared is running and which hostnames a3watch expects.",
    }


def access_domains_map(cfg: Config) -> dict:
    return {
        "access": {
            "enabled": cfg.cf_access_enabled,
            "team_domain": cfg.cf_access_team_domain,
            "aud": (cfg.cf_access_aud[:8] + "…") if cfg.cf_access_aud else "",
        },
        "hostnames": sorted(set(filter(None, [cfg.tunnel_hostname]))),
        "cors_allow_origins": list(cfg.allow_origins),
        "api_bind": f"{cfg.api_bind}:{cfg.api_port}",
    }


# ---- secret inventory (path/mode/owner only, never values) -----------------
# filenames that hold secrets — flagged by name so the user sees them, value NEVER read
_SECRET_FILE = re.compile(
    r"(\.(key|pem|crt|p12|pfx|kdbx)$|(^|[._-])env$|id_(rsa|ed25519|ecdsa)|"
    r"(token|secret|credential|password|htpasswd)|cloudflare|\.npmrc$|\.pgpass$)", re.I)


def _scan_secret_files(home: str, limit: int = 60) -> list[str]:
    """Paths of secret-looking files under ~/ (NVMe-bounded, names only)."""
    hits: list[str] = []
    for dirpath, dirnames, filenames in _walk_bounded(home, max_entries=4000, want_files=True):
        # skip heavy/noise + tool dirs (.claude plugins etc.) — we want the user's
        # own infra secrets, not tooling artifacts
        dirnames[:] = [d for d in dirnames if d not in _COLLAPSE and d != ".claude"]
        for f in filenames:
            if _SECRET_FILE.search(f):
                hits.append(os.path.join(dirpath, f))
                if len(hits) >= limit:
                    return hits
    return hits


def secrets_inventory(cfg: Config, home: str) -> list[dict]:
    candidates = [
        os.path.join(cfg.data_dir, "token"),
        "/etc/a3watch/config.toml",
        os.path.join(home, ".ssh"),
        os.path.join(home, ".cloudflared"),
        "/etc/cloudflared",
    ]
    candidates += _scan_secret_files(home)
    out: list[dict] = []
    seen: set = set()
    for p in candidates:
        if p in seen:
            continue
        seen.add(p)
        try:
            st = os.stat(p)
        except OSError:
            continue
        try:
            owner = pwd.getpwuid(st.st_uid).pw_name
        except KeyError:
            owner = str(st.st_uid)
        world = bool(st.st_mode & (stat.S_IROTH | stat.S_IWOTH))
        out.append({
            "path": p,
            "kind": "dir" if stat.S_ISDIR(st.st_mode) else "file",
            "mode": oct(st.st_mode & 0o777),
            "owner": owner,
            "world_accessible": world,
            "note": "secret value NOT read/exposed" if not world else
                    "WORLD-ACCESSIBLE — consider tightening perms",
        })
    return out


# ---- ~/ file tree (NVMe-bounded, metadata only) ----------------------------
# noise dirs: summarise (size + count) rather than expanding them file-by-file
_COLLAPSE = {"node_modules", ".git", ".cache", ".npm", "__pycache__", ".svelte-kit",
             "build", "dist", ".venv", "venv", ".pnpm-store", ".local"}
_MAX_NODES = 4000


def _walk_bounded(root: str, max_entries: int, want_files: bool = False):
    """os.walk on the home FILESYSTEM only — never crosses a mount boundary (so it
    can't descend into a spun-down HDD pool) and never follows symlinks. Yields
    (dirpath, dirnames, filenames); caller may prune dirnames in place."""
    try:
        root_dev = os.stat(root).st_dev
    except OSError:
        return
    count = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # prune any subdir on a different device (a mountpoint) — stays on NVMe
        keep = []
        for d in dirnames:
            try:
                if os.stat(os.path.join(dirpath, d)).st_dev == root_dev:
                    keep.append(d)
            except OSError:
                continue
        dirnames[:] = keep
        yield dirpath, dirnames, filenames
        count += 1
        if count >= max_entries:
            return


def home_tree(home: str) -> dict:
    """A nested size/count inventory of ~/, metadata only (no contents). Noise
    dirs are collapsed to a summary. NON-WAKING: stays on the NVMe home fs."""
    try:
        root_dev = os.stat(home).st_dev
    except OSError:
        return {"error": "home not readable"}

    nodes = 0
    total_files = 0
    total_bytes = 0

    def build(path: str, depth: int) -> dict:
        nonlocal nodes, total_files, total_bytes
        node: dict = {"name": os.path.basename(path) or path, "type": "dir",
                      "size": 0, "files": 0, "children": []}
        try:
            entries = sorted(os.scandir(path), key=lambda e: e.name)
        except OSError:
            return node
        for e in entries:
            if nodes >= _MAX_NODES:
                node["truncated"] = True
                break
            try:
                if e.is_symlink():
                    node["children"].append({"name": e.name, "type": "symlink"})
                    nodes += 1
                    continue
                st = e.stat()
                if e.is_dir():
                    if st.st_dev != root_dev:
                        node["children"].append({"name": e.name, "type": "mount",
                                                 "note": "separate filesystem — not traversed"})
                        nodes += 1
                        continue
                    if e.name in _COLLAPSE:
                        sz, fc = _dir_summary(e.path, root_dev)
                        node["children"].append({"name": e.name, "type": "dir",
                                                 "size": sz, "files": fc, "collapsed": True})
                        node["size"] += sz
                        node["files"] += fc
                        total_bytes += sz
                        total_files += fc
                        nodes += 1
                        continue
                    child = build(e.path, depth + 1) if depth < 5 else _leaf_dir(e.path, root_dev)
                    node["children"].append(child)
                    node["size"] += child.get("size", 0)
                    node["files"] += child.get("files", 0)
                    nodes += 1
                else:
                    node["children"].append({"name": e.name, "type": "file",
                                             "size": st.st_size, "mtime": int(st.st_mtime)})
                    node["size"] += st.st_size
                    node["files"] += 1
                    total_files += 1
                    total_bytes += st.st_size
                    nodes += 1
            except OSError:
                continue
        return node

    root = build(home, 0)
    return {"root": root, "total_files": total_files, "total_bytes": total_bytes,
            "nodes_shown": nodes, "capped": nodes >= _MAX_NODES}


def _dir_summary(path: str, root_dev: int):
    size = files = 0
    for dp, dirs, fnames in os.walk(path, followlinks=False):
        dirs[:] = [d for d in dirs
                   if _same_dev(os.path.join(dp, d), root_dev)]
        for f in fnames:
            try:
                st = os.stat(os.path.join(dp, f))
                size += st.st_size
                files += 1
            except OSError:
                continue
    return size, files


def _leaf_dir(path: str, root_dev: int):
    sz, fc = _dir_summary(path, root_dev)
    return {"name": os.path.basename(path), "type": "dir", "size": sz, "files": fc,
            "collapsed": True}


def _same_dev(path: str, root_dev: int) -> bool:
    try:
        return os.stat(path).st_dev == root_dev
    except OSError:
        return False


# ---- assemble --------------------------------------------------------------
def build_map(cfg: Config, home: str = "/home/chris") -> dict:
    docker = _safe(lambda: docker_map(home), {"containers": [], "networks": [], "compose_files": []})
    containers = docker.get("containers", [])
    return {
        "home": home,
        "git": _safe(lambda: git_map(home), []),
        "docker": docker,
        "tunnel": _safe(lambda: tunnel_map(cfg, containers), {}),
        "access_domains": _safe(lambda: access_domains_map(cfg), {}),
        "systemd": _safe(systemd_map, {"units": []}),
        "secrets": _safe(lambda: secrets_inventory(cfg, home), []),
        "home_tree": _safe(lambda: home_tree(home), {}),
        "pipelines": _pipelines(cfg),
        "cloud_snapshot": None,  # filled by a one-off Cloudflare/GitHub token run
    }


def _pipelines(cfg: Config) -> list[dict]:
    host = cfg.tunnel_hostname or "a3.chrisj.uk"
    return [
        {"name": "Web request",
         "steps": [f"browser → https://{host}",
                   f"Cloudflare edge: Access auth (team {cfg.cf_access_team_domain or '?'})",
                   "cloudflared tunnel → cloudflared container",
                   f"→ a3watch agent {cfg.api_bind}:{cfg.api_port} (ufw-allowed docker subnet)",
                   "agent (socket-activated) serves the built SPA + /api"]},
        {"name": "Deploy",
         "steps": ["edit locally", "git push → main (GitHub)",
                   "build SPA (npm run build)",
                   "SPA → /var/lib/a3watch/web, agent code → /opt/a3watch",
                   "systemctl restart a3watch-api.socket"]},
        {"name": "Observability sampling",
         "steps": [f"a3watch-sample.timer ({cfg.interval_s}s)", "sample.py (non-waking reads)",
                   "SQLite (WAL) on NVMe", "read-only /api"]},
    ]


def _safe(fn, default):
    try:
        return fn()
    except Exception:
        return default
