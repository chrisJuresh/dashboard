"""
a3watch.cicd — is each deployment current with GitHub, mid-deploy, or behind?

Read-only, non-waking. For CI-driven apps (films, which) it reads the *public*
GitHub API (latest commit + latest Actions run on the default branch) and
compares them to the running container: a workflow in progress = deploying; a
failed run = CI failed; otherwise the deployment is "up to date" if the running
container was created after the latest commit, else "out of date" (built/committed
but not yet rolled out — Watchtower pulls within its interval). The manually
deployed dashboard compares its local checkout to origin/main instead.

No token: these repos are public. Unauthenticated GitHub API is rate-limited
(60/h), so check() is cached briefly and the tab only fetches on open.
"""
from __future__ import annotations

import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

from . import util

# (display name, github owner/repo, deploy kind, container OR local path)
DEPLOYABLES = [
    {"name": "films",     "repo": "chrisJuresh/films",     "kind": "ci", "container": "films"},
    {"name": "which",     "repo": "chrisJuresh/which",     "kind": "ci", "container": "which"},
    {"name": "dashboard", "repo": "chrisJuresh/dashboard", "kind": "manual", "path": "/home/chris/dashboard"},
]

_GH = "https://api.github.com"
_cache: dict = {"ts": 0.0, "data": None}
_CACHE_TTL = 120.0


def _iso(s: str):
    """ISO-8601 (GitHub uses Z) -> epoch seconds, or None."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _gh(path: str):
    """Unauthenticated GitHub API GET -> (json, error_str). Flags rate limiting."""
    req = urllib.request.Request(_GH + path, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "a3watch-cicd",
    })
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            return json.load(r), None
    except urllib.error.HTTPError as e:
        if e.code == 403 and e.headers.get("X-RateLimit-Remaining") == "0":
            return None, "github rate-limited (unauthenticated 60/h) — try again shortly"
        return None, f"github HTTP {e.code}"
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        return None, f"github unreachable: {e}"


def _container_created(name: str):
    rc, out, _ = util.run_cmd(["docker", "inspect", name, "--format", "{{.Created}}"],
                              timeout=6.0, allow_diagnostic=True)
    return _iso(out.strip()) if rc == 0 else None


def _check_ci(d: dict) -> dict:
    owner_repo = d["repo"]
    res = {"name": d["name"], "repo": owner_repo, "kind": "ci"}
    commit, e1 = _gh(f"/repos/{owner_repo}/commits/HEAD")
    if e1:
        return {**res, "state": "unknown", "detail": e1}
    csha = commit.get("sha", "")[:7]
    cmsg = ((commit.get("commit") or {}).get("message") or "").splitlines()[0]
    cdate = _iso(((commit.get("commit") or {}).get("committer") or {}).get("date", ""))
    res.update({"latest_commit": csha, "commit_msg": cmsg, "commit_ts": cdate})

    runs, e2 = _gh(f"/repos/{owner_repo}/actions/runs?per_page=1")
    run = (runs or {}).get("workflow_runs", [None])[0] if not e2 else None
    if run:
        res["run"] = {
            "status": run.get("status"), "conclusion": run.get("conclusion"),
            "head": (run.get("head_sha") or "")[:7], "url": run.get("html_url"),
            "at": _iso(run.get("updated_at", "")),
        }

    created = _container_created(d["container"])
    res["deployed_at"] = created

    # ---- state machine (keyed off the BUILD, not the commit: a commit that
    # doesn't rebuild the image — docs, compose, CI config — must not read as
    # "out of date") ----
    run_at = res.get("run", {}).get("at")
    run_head = res.get("run", {}).get("head")
    if run and run.get("status") in ("queued", "in_progress", "waiting", "requested", "pending"):
        res.update(state="deploying", detail=f"workflow {run.get('status')} for {run_head}")
    elif run and run.get("conclusion") in ("failure", "timed_out", "startup_failure", "cancelled"):
        res.update(state="ci_failed", detail="latest workflow run failed")
    elif created is None:
        res.update(state="unknown", detail=f"container '{d['container']}' not found")
    elif run_at and created < run_at - 120:
        res.update(state="out_of_date",
                   detail="a newer image was built after the running container — Watchtower rollout pending")
    else:
        res.update(state="up_to_date", detail="running the latest built image")
    # informational: a commit exists that the last build didn't cover
    if res.get("state") == "up_to_date" and run_head and csha and run_head != csha:
        res["detail"] += f" · latest commit {csha} not built (last build {run_head} — likely a non-app change)"
    return res


def _check_manual(d: dict) -> dict:
    repo = d["path"]
    res = {"name": d["name"], "repo": d["repo"], "kind": "manual"}

    def git(*a):
        rc, out, _ = util.run_cmd(["git", "-C", repo, *a], timeout=8.0, allow_diagnostic=True)
        return out.strip() if rc == 0 else ""

    git("fetch", "--quiet", "origin")
    head = git("rev-parse", "--short", "HEAD")
    origin = git("rev-parse", "--short", "origin/main")
    behind = git("rev-list", "--count", "HEAD..origin/main")
    last = git("log", "-1", "--format=%h %s")
    res.update(local_head=head, origin_head=origin, behind=int(behind) if behind.isdigit() else 0,
               latest_commit=origin, commit_msg=last)
    if not head or not origin:
        res.update(state="unknown", detail="git state unreadable")
    elif res["behind"] > 0:
        res.update(state="out_of_date", detail=f"local checkout is {res['behind']} commit(s) behind origin/main — pull + rebuild")
    else:
        res.update(state="up_to_date", detail="checkout matches origin/main (built + deployed manually)")
    return res


def check(force: bool = False) -> dict:
    now = time.time()
    if not force and _cache["data"] is not None and (now - _cache["ts"]) < _CACHE_TTL:
        return {**_cache["data"], "cached": True}
    items = []
    for d in DEPLOYABLES:
        try:
            items.append(_check_ci(d) if d["kind"] == "ci" else _check_manual(d))
        except Exception as e:  # never let one repo break the panel
            items.append({"name": d["name"], "repo": d["repo"], "state": "unknown", "detail": str(e)})
    order = {"deploying": 0, "ci_failed": 1, "out_of_date": 2, "unknown": 3, "up_to_date": 4}
    items.sort(key=lambda i: order.get(i.get("state"), 9))
    summary = {
        "all_up_to_date": all(i.get("state") == "up_to_date" for i in items),
        "deploying": sum(1 for i in items if i.get("state") == "deploying"),
        "out_of_date": sum(1 for i in items if i.get("state") == "out_of_date"),
        "ci_failed": sum(1 for i in items if i.get("state") == "ci_failed"),
    }
    data = {"generated": int(now), "summary": summary, "items": items, "cached": False}
    _cache.update(ts=now, data=data)
    return data
