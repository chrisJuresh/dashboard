"""
a3watch.api — read-only JSON API (stdlib http.server), socket-activated.

  * Socket activation: systemd passes the listening socket on fd 3 ($LISTEN_FDS)
    so the service uses ZERO resources until the first request, and it idle-exits
    after cfg.api_idle_exit_s. Falls back to binding cfg.api_bind:port directly.
  * Auth: Authorization: Bearer <token> (constant-time compare) on every route
    except /api/health.
  * CORS: reflects the request Origin iff it is in cfg.allow_origins (the Vercel
    URL), so the static SPA on Vercel can read the tunnelled API.
  * Every data route is SELECT-only against a read-only SQLite handle. No route
    probes a disk. The only writes are the explicit, authed /api/diag/* routes.
"""

from __future__ import annotations

import json
import mimetypes
import os
import socket
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

try:
    import jwt  # PyJWT (system package) — used only to verify Cloudflare Access tokens
except ImportError:  # pragma: no cover
    jwt = None

from . import __version__, diag
from .config import Config

_last_request = [time.time()]
_cf_jwks: dict = {}  # team_domain -> jwt.PyJWKClient (cached across requests)


def verify_cf_access(assertion: str, team_domain: str, aud: str):
    """Return the decoded Cloudflare Access JWT claims (a dict) if valid, else None.
    Cloudflare injects the Cf-Access-Jwt-Assertion header on requests it has
    authenticated; verifying its signature/aud/issuer proves the caller passed
    the Access login (and can't be forged by, e.g., a sibling container)."""
    if not (jwt and assertion and team_domain and aud):
        return None
    try:
        client = _cf_jwks.get(team_domain)
        if client is None:
            client = jwt.PyJWKClient(f"https://{team_domain}/cdn-cgi/access/certs")
            _cf_jwks[team_domain] = client
        signing_key = client.get_signing_key_from_jwt(assertion)
        return jwt.decode(
            assertion, signing_key.key, algorithms=["RS256"],
            audience=aud, issuer=f"https://{team_domain}",
        )
    except Exception:
        return None


def _ro_conn(cfg: Config) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _one(conn, sql, params=()):
    r = conn.execute(sql, params).fetchone()
    return dict(r) if r else None


# --------------------------------------------------------- data queries -----
def q_status(cfg: Config, conn) -> dict:
    latest = _one(conn, "SELECT MAX(ts) t FROM sample")
    ts = (latest or {}).get("t") or 0
    disks = []
    for d in _rows(conn, "SELECT * FROM disk ORDER BY rotational DESC, dev"):
        last = _one(conn,
                    "SELECT power_state, active FROM disk_sample WHERE dev=? ORDER BY ts DESC LIMIT 1",
                    (d["dev"],))
        state = (last or {}).get("power_state", "unknown")
        # how long has the current state held?
        hist = _rows(conn,
                     "SELECT ts, power_state FROM disk_sample WHERE dev=? ORDER BY ts DESC LIMIT 500",
                     (d["dev"],))
        changed = hist[0]["ts"] if hist else ts
        for h in hist:
            if h["power_state"] != state:
                break
            changed = h["ts"]
        recent = _one(conn,
                      "SELECT COALESCE(SUM(reads_d),0) r, COALESCE(SUM(writes_d),0) w "
                      "FROM disk_sample WHERE dev=? AND ts > ?", (d["dev"], ts - 900))
        disks.append({
            "dev": d["dev"], "role": d["role"], "model": d["model"], "mount": d["mount"],
            "label": d["label"], "rotational": bool(d["rotational"]),
            "protected": bool(d["protected"]), "power_state": state,
            "active": bool((last or {}).get("active", 0)),
            "minutes_in_state": round((ts - changed) / 60.0, 1),
            "reads_recent": (recent or {}).get("r", 0), "writes_recent": (recent or {}).get("w", 0),
        })
    pkg = _one(conn, "SELECT watts FROM cpu_power WHERE ts=? AND domain='package'", (ts,))
    core = _one(conn, "SELECT watts FROM cpu_power WHERE ts=? AND domain='core'", (ts,))
    busy = _one(conn, "SELECT busy_pct FROM cpu_busy WHERE ts=?", (ts,))
    pkg_cs = _rows(conn, "SELECT name, residency_pct FROM cstate WHERE ts=? AND scope='package'", (ts,))
    core_cs = _rows(conn, "SELECT name, residency_pct FROM cstate WHERE ts=? AND scope='core'", (ts,))
    deep = sum(c["residency_pct"] for c in pkg_cs if c["name"] in ("PC6", "PC8", "PC10"))
    ov = _one(conn, "SELECT * FROM overhead ORDER BY ts DESC LIMIT 1") or {}
    gbp = ov.get("gbp_year", 0.0)
    open_ev = _one(conn, "SELECT COUNT(*) c FROM disk_event WHERE ts > ?", (ts - 86400,))
    stray = _one(conn, "SELECT COUNT(DISTINCT pid||flag) c FROM proc_flag WHERE ts > ?", (ts - 120,))
    return {
        "ts": ts, "mode": "diagnostic" if diag.is_running(cfg) else "normal",
        "disks": disks,
        "cpu": {
            "pkg_w": (pkg or {}).get("watts"), "core_w": (core or {}).get("watts"),
            "busy_pct": (busy or {}).get("busy_pct", 0.0),
            "pkg_cstates": [{"name": c["name"], "pct": c["residency_pct"]} for c in pkg_cs],
            "core_cstates": [{"name": c["name"], "pct": c["residency_pct"]} for c in core_cs],
            "pkg_deep_ok": deep > 1.0,
        },
        "overhead": {
            "avg_watts": ov.get("avg_watts", 0.0), "gbp_year": gbp,
            "budget_gbp": cfg.budget_gbp_year, "db_mb": round(ov.get("db_bytes", 0) / 1e6, 2),
            "samples": ov.get("samples", 0), "cpu_ms_day": ov.get("cpu_ms_day", 0.0),
            "within_budget": gbp <= cfg.budget_gbp_year,
        },
        "counts": {"open_disk_events": (open_ev or {}).get("c", 0),
                   "stray_procs": (stray or {}).get("c", 0)},
    }


def q_disk_events(conn, since, limit, dev) -> dict:
    sql = "SELECT * FROM disk_event WHERE ts > ?"
    params = [since]
    if dev:
        sql += " AND dev=?"
        params.append(dev)
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(min(limit, 1000))
    events = _rows(conn, sql, tuple(params))
    for e in events:
        e["evidence"] = _rows(conn,
                              "SELECT signal, detail, weight FROM event_evidence WHERE event_id=?",
                              (e["id"],))
    return {"events": events}


def q_power_series(conn, frm, to, res) -> dict:
    if res == "hour":
        rows = _rows(conn,
                     "SELECT hour*3600 ts, domain, avg_w w FROM cpu_power_hourly "
                     "WHERE hour>=? AND hour<=? ORDER BY hour", (int(frm // 3600), int(to // 3600)))
    else:
        rows = _rows(conn,
                     "SELECT ts, domain, watts w FROM cpu_power WHERE ts>=? AND ts<=? ORDER BY ts",
                     (frm, to))
    byts: dict = {}
    for r in rows:
        p = byts.setdefault(r["ts"], {"ts": r["ts"], "pkg_w": None, "core_w": None,
                                      "uncore_w": None, "dram_w": None})
        key = {"package": "pkg_w", "core": "core_w", "uncore": "uncore_w", "dram": "dram_w"}.get(r["domain"])
        if key:
            p[key] = r["w"]
    return {"points": [byts[k] for k in sorted(byts)]}


def q_cstate_series(conn, frm, to, scope, res) -> dict:
    if res == "hour":
        rows = _rows(conn,
                     "SELECT hour*3600 ts, name, avg_pct pct FROM cstate_hourly "
                     "WHERE scope=? AND hour>=? AND hour<=? ORDER BY hour",
                     (scope, int(frm // 3600), int(to // 3600)))
    else:
        rows = _rows(conn,
                     "SELECT ts, name, residency_pct pct FROM cstate "
                     "WHERE scope=? AND ts>=? AND ts<=? ORDER BY ts", (scope, frm, to))
    states: list[str] = []
    byts: dict = {}
    for r in rows:
        if r["name"] not in states:
            states.append(r["name"])
        byts.setdefault(r["ts"], {"ts": r["ts"]})[r["name"]] = r["pct"]
    return {"scope": scope, "states": states, "points": [byts[k] for k in sorted(byts)]}


def q_disk_series(conn, dev, frm, to, res) -> dict:
    rows = _rows(conn,
                 "SELECT ts, active, reads_d, writes_d, power_state FROM disk_sample "
                 "WHERE dev=? AND ts>=? AND ts<=? ORDER BY ts", (dev, frm, to))
    return {"dev": dev, "points": rows}


def q_processes(conn) -> dict:
    latest = _one(conn, "SELECT MAX(ts) t FROM sample")
    ts = (latest or {}).get("t") or 0
    flags = _rows(conn, "SELECT pid, comm, cgroup, flag, note FROM proc_flag WHERE ts > ?", (ts - 120,))
    top = _rows(conn, "SELECT pid, comm, cgroup, cpu_pct FROM cpu_top WHERE ts=? ORDER BY cpu_pct DESC", (ts,))
    io = _rows(conn, "SELECT pid, read_bytes_d, write_bytes_d FROM proc_io WHERE ts=?", (ts,))
    io_by_pid = {r["pid"]: r for r in io}
    procs: dict = {}
    for f in flags:
        p = procs.setdefault(f["pid"], {"pid": f["pid"], "comm": f["comm"], "cgroup": f["cgroup"],
                                        "cpu_pct": 0.0, "read_bytes_d": 0, "write_bytes_d": 0,
                                        "flags": [], "note": f["note"]})
        if f["flag"] not in p["flags"]:  # same flag can recur across cycles in the window
            p["flags"].append(f["flag"])
    for t in top:
        p = procs.setdefault(t["pid"], {"pid": t["pid"], "comm": t["comm"], "cgroup": t["cgroup"],
                                        "cpu_pct": t["cpu_pct"], "read_bytes_d": 0,
                                        "write_bytes_d": 0, "flags": [], "note": ""})
        p["cpu_pct"] = t["cpu_pct"]
        if t["pid"] in io_by_pid:
            p["read_bytes_d"] = io_by_pid[t["pid"]]["read_bytes_d"]
            p["write_bytes_d"] = io_by_pid[t["pid"]]["write_bytes_d"]
    return {"procs": sorted(procs.values(), key=lambda p: (len(p["flags"]), p["cpu_pct"]), reverse=True)}


def q_power_events(conn, since, limit) -> dict:
    return {"events": _rows(conn,
                            "SELECT * FROM power_event WHERE ts > ? ORDER BY ts DESC LIMIT ?",
                            (since, min(limit, 1000)))}


def q_overhead(conn, cfg, frm, to) -> dict:
    pts = _rows(conn, "SELECT * FROM overhead WHERE ts>=? AND ts<=? ORDER BY ts", (frm, to))
    cur = _one(conn, "SELECT * FROM overhead ORDER BY ts DESC LIMIT 1") or {}
    return {"points": pts, "current": cur, "budget_gbp": cfg.budget_gbp_year}


def q_metrics_latest(conn) -> dict:
    rows = _rows(conn, "SELECT collector, grp, key, num, txt, unit, ts FROM metric_latest "
                       "ORDER BY grp, collector, key")
    groups: dict = {}
    latest_ts = 0.0
    for r in rows:
        latest_ts = max(latest_ts, r["ts"] or 0)
        g = groups.setdefault(r["grp"] or "Other", [])
        g.append({"collector": r["collector"], "key": r["key"], "num": r["num"],
                  "txt": r["txt"], "unit": r["unit"], "ts": r["ts"]})
    return {"ts": latest_ts, "groups": [{"group": k, "metrics": v} for k, v in groups.items()]}


def q_metric_series(conn, key, frm, to, res) -> dict:
    if res == "hour":
        pts = _rows(conn, "SELECT hour*3600 ts, avg_num num, min_num, max_num FROM metric_series_hourly "
                          "WHERE key=? AND hour>=? AND hour<=? ORDER BY hour",
                    (key, int(frm // 3600), int(to // 3600)))
    else:
        pts = _rows(conn, "SELECT ts, num FROM metric_series WHERE key=? AND ts>=? AND ts<=? ORDER BY ts",
                    (key, frm, to))
    return {"key": key, "points": pts}


def q_config(cfg: Config) -> dict:
    return {
        "interval_s": cfg.interval_s, "budget_gbp_year": cfg.budget_gbp_year,
        "data_dir": cfg.data_dir, "tunnel_hostname": cfg.tunnel_hostname,
        "mode": "diagnostic" if diag.is_running(cfg) else "normal",
        "disks": [{"dev": d.dev, "role": d.role, "label": d.label, "mount": d.mount,
                   "rotational": d.rotational, "protected": d.protected,
                   "monitored": d.monitored, "pool": d.pool} for d in cfg.disks],
    }


# ------------------------------------------------------------- handler ------
def make_handler(cfg: Config, token: str):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):  # silence default stderr logging
            pass

        def _cors(self):
            origin = self.headers.get("Origin", "")
            if origin and origin in cfg.allow_origins:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

        def _send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)

        def _access_email(self):
            """Email from a valid Cloudflare Access JWT, else None."""
            if not cfg.cf_access_enabled:
                return None
            claims = verify_cf_access(self.headers.get("Cf-Access-Jwt-Assertion", ""),
                                      cfg.cf_access_team_domain, cfg.cf_access_aud)
            return (claims.get("email") or "authenticated") if claims else None

        def _authed(self) -> bool:
            import hmac
            # (1) Cloudflare Access: a valid Access JWT injected by the edge after login.
            if self._access_email():
                return True
            # (2) Bearer token: for direct/CLI/automation use (not exposed in the UI).
            hdr = self.headers.get("Authorization", "")
            got = hdr[7:] if hdr.startswith("Bearer ") else ""
            return bool(token) and hmac.compare_digest(got, token)

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            _last_request[0] = time.time()
            u = urlparse(self.path)
            path, qs = u.path, parse_qs(u.query)
            if path == "/api/health":
                return self._send(200, {"ok": True, "version": __version__, "ts": time.time(),
                                        "mode": "diagnostic" if diag.is_running(cfg) else "normal"})
            if path == "/api/session":  # informational: who is signed in (via Cloudflare Access)
                email = self._access_email()
                return self._send(200, {"authenticated": bool(email), "email": email})
            if not path.startswith("/api/"):
                # Serve the built SPA (or the fallback page). Static assets carry no
                # secrets and the hostname is gated by Cloudflare Access at the edge.
                return self._serve_static(path)
            if not self._authed():
                return self._send(401, {"error": "unauthorized"})
            try:
                return self._route_get(path, qs)
            except Exception as e:  # never leak internals / crash the server
                sys.stderr.write(f"a3watch api error on {path}: {e}\n")
                return self._send(500, {"error": "internal error"})

        def _num(self, qs, key, default):
            try:
                return float(qs.get(key, [default])[0])
            except (ValueError, TypeError):
                return default

        def _route_get(self, path, qs):
            if path.startswith("/api/diag/"):
                return self._route_diag_get(path)
            conn = _ro_conn(cfg)
            try:
                if path == "/api/status":
                    return self._send(200, q_status(cfg, conn))
                if path == "/api/disks/events":
                    return self._send(200, q_disk_events(conn, self._num(qs, "since", 0),
                                                         int(self._num(qs, "limit", 200)),
                                                         (qs.get("dev", [""])[0])))
                if path == "/api/timeseries/power":
                    return self._send(200, q_power_series(conn, self._num(qs, "from", 0),
                                                          self._num(qs, "to", time.time()),
                                                          qs.get("res", ["raw"])[0]))
                if path == "/api/timeseries/cstate":
                    return self._send(200, q_cstate_series(conn, self._num(qs, "from", 0),
                                                           self._num(qs, "to", time.time()),
                                                           qs.get("scope", ["package"])[0],
                                                           qs.get("res", ["raw"])[0]))
                if path == "/api/timeseries/disk":
                    return self._send(200, q_disk_series(conn, qs.get("dev", [""])[0],
                                                         self._num(qs, "from", 0),
                                                         self._num(qs, "to", time.time()),
                                                         qs.get("res", ["raw"])[0]))
                if path == "/api/processes":
                    return self._send(200, q_processes(conn))
                if path == "/api/power/events":
                    return self._send(200, q_power_events(conn, self._num(qs, "since", 0),
                                                          int(self._num(qs, "limit", 200))))
                if path == "/api/overhead":
                    return self._send(200, q_overhead(conn, cfg, self._num(qs, "from", 0),
                                                      self._num(qs, "to", time.time())))
                if path == "/api/metrics/latest":
                    return self._send(200, q_metrics_latest(conn))
                if path == "/api/metrics/series":
                    return self._send(200, q_metric_series(conn, qs.get("key", [""])[0],
                                                           self._num(qs, "from", 0),
                                                           self._num(qs, "to", time.time()),
                                                           qs.get("res", ["raw"])[0]))
                if path == "/api/config":
                    return self._send(200, q_config(cfg))
                return self._send(404, {"error": "not found"})
            finally:
                conn.close()

        def _route_diag_get(self, path):
            if path == "/api/diag/status":
                return self._send(200, diag.status(cfg))
            if path.startswith("/api/diag/result/"):
                sid = path.rsplit("/", 1)[-1]
                return self._send(200, diag.result(cfg, sid))
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            _last_request[0] = time.time()
            u = urlparse(self.path)
            if not self._authed():
                return self._send(401, {"error": "unauthorized"})
            if u.path != "/api/diag/start":
                return self._send(404, {"error": "not found"})
            length = int(self.headers.get("Content-Length", 0) or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._send(400, {"error": "bad json"})
            try:
                sid = diag.start(cfg, body.get("tool", ""), int(body.get("seconds", 10)),
                                 body.get("dev"), bool(body.get("confirm_wake", False)))
                return self._send(200, {"session_id": sid})
            except diag.DiagError as e:
                return self._send(400, {"error": str(e)})

        def _serve_static(self, path: str):
            """Serve the built SPA from cfg.web_dir with path-traversal protection and
            SPA fallback to index.html. Falls back to the built-in page if no SPA is
            installed."""
            root = os.path.realpath(cfg.web_dir)
            rel = path.lstrip("/") or "index.html"
            # refuse dotfiles/dotdirs outright (defense-in-depth)
            if any(seg.startswith(".") for seg in rel.split("/") if seg):
                return self._send(403, {"error": "forbidden"})
            full = os.path.realpath(os.path.join(root, rel))
            # containment check — never serve outside web_dir
            if full != root and not full.startswith(root + os.sep):
                return self._send(403, {"error": "forbidden"})
            # never serve the token/db even if web_root is mis-set to contain them
            if full in (os.path.realpath(cfg.token_path), os.path.realpath(cfg.db_path)):
                return self._send(403, {"error": "forbidden"})
            if not os.path.isfile(full):
                # client-side route (no file extension) → SPA shell; else 404
                index = os.path.join(root, "index.html")
                if "." not in os.path.basename(rel) and os.path.isfile(index):
                    full = index
                elif os.path.isfile(index):
                    full = index
                else:
                    return self._fallback_page()
            try:
                with open(full, "rb") as fh:
                    body = fh.read()
            except OSError:
                return self._fallback_page()
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            # immutable hashed assets can cache; the shell should not
            if "/_app/immutable/" in path:
                self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            else:
                self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def _fallback_page(self):
            html = _FALLBACK_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

    return Handler


_FALLBACK_HTML = """<!doctype html><meta charset=utf-8><title>a3watch</title>
<style>body{font:14px system-ui;margin:40px;background:#0d0d0d;color:#eee;max-width:640px}
code{background:#222;padding:2px 6px;border-radius:4px}a{color:#3987e5}</style>
<h1>a3watch agent</h1>
<p>The agent is running and its read-only API is live. The full dashboard is the
SvelteKit app (hosted on Vercel); point it at this API's URL with your bearer token.</p>
<p>Quick checks: <a href="/api/health">/api/health</a></p>
<p>All API routes except <code>/api/health</code> require
<code>Authorization: Bearer &lt;token&gt;</code>.</p>
"""


# --------------------------------------------------------------- serve ------
def serve(cfg: Config) -> None:
    token = ""
    try:
        with open(cfg.token_path) as fh:
            token = fh.read().strip()
    except OSError:
        pass
    handler = make_handler(cfg, token)

    listen_fds = int(os.environ.get("LISTEN_FDS", "0") or "0")
    if listen_fds > 0:
        sock = socket.socket(fileno=3)  # systemd socket activation
        httpd = ThreadingHTTPServer((cfg.api_bind, cfg.api_port), handler, bind_and_activate=False)
        httpd.socket = sock
    else:
        httpd = ThreadingHTTPServer((cfg.api_bind, cfg.api_port), handler)

    # idle-exit watchdog: exit after the API has been quiet, so nothing lingers.
    # Long sleeps keep CPU wakeups rare even while a dashboard is open — real
    # requests wake select() immediately regardless of poll_interval.
    def watchdog():
        while True:
            time.sleep(30)
            if time.time() - _last_request[0] > cfg.api_idle_exit_s:
                httpd.shutdown()
                return

    _last_request[0] = time.time()
    threading.Thread(target=watchdog, daemon=True).start()
    try:
        httpd.serve_forever(poll_interval=30.0)
    finally:
        httpd.server_close()
