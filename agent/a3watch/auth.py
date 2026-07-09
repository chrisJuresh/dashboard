"""
a3watch.auth — self-hosted login (no third party, no billing).

A single account (email + password) protects the dashboard. Passwords are hashed
with scrypt; sessions are stateless HMAC-signed cookies keyed by a per-install
secret. Everything lives in <data_dir>/auth.json (0600) on the NVMe. Set/change
the password on the server with `sudo a3watch set-login --email <you>`; the web
side only ever *logs in*, never sets the password (so a stranger hitting the
public URL can't claim the account).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

# scrypt params: ~16 MiB, interactive-login cost.
_N, _R, _P, _DK = 16384, 8, 1, 32
_MAXMEM = 64 * 1024 * 1024
SESSION_TTL = 30 * 24 * 3600  # 30 days


def auth_path(cfg) -> str:
    return os.path.join(cfg.data_dir, "auth.json")


def load_auth(cfg) -> dict:
    try:
        with open(auth_path(cfg)) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def login_configured(cfg) -> bool:
    return bool(load_auth(cfg).get("scrypt_hash"))


def _hash_pw(password: str, salt: bytes) -> str:
    return hashlib.scrypt(
        password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=_DK, maxmem=_MAXMEM
    ).hex()


def set_login(cfg, email: str, password: str) -> None:
    """Create/update the single account. Preserves the session secret so an
    existing password change doesn't necessarily nuke sessions (we rotate it
    only when absent)."""
    a = load_auth(cfg)
    salt = os.urandom(16)
    a["email"] = email.strip()
    a["scrypt_salt"] = salt.hex()
    a["scrypt_hash"] = _hash_pw(password, salt)
    if not a.get("session_secret"):
        a["session_secret"] = secrets.token_hex(32)
    os.makedirs(cfg.data_dir, exist_ok=True)
    fd = os.open(auth_path(cfg), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(a, fh)


def verify_password(cfg, email: str, password: str) -> bool:
    a = load_auth(cfg)
    if not a.get("scrypt_hash"):
        return False
    email_ok = hmac.compare_digest(email.strip().lower(), a.get("email", "").lower())
    try:
        cand = _hash_pw(password, bytes.fromhex(a["scrypt_salt"]))
    except (ValueError, KeyError):
        return False
    pw_ok = hmac.compare_digest(cand, a["scrypt_hash"])
    # evaluate both (no early-out) then AND — avoids leaking which was wrong
    return email_ok and pw_ok


def make_session(cfg, email: str, ttl: int = SESSION_TTL) -> str | None:
    a = load_auth(cfg)
    sec = a.get("session_secret", "")
    if not sec:
        return None
    payload = base64.urlsafe_b64encode(
        json.dumps({"e": email, "x": int(time.time()) + ttl}).encode()
    ).decode().rstrip("=")
    sig = hmac.new(bytes.fromhex(sec), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_session(cfg, cookie_val: str) -> str | None:
    """Return the authenticated email, or None. Checks HMAC, expiry, and that the
    session's email still matches the configured account."""
    if not cookie_val or "." not in cookie_val:
        return None
    a = load_auth(cfg)
    sec = a.get("session_secret", "")
    if not sec:
        return None
    payload, _, sig = cookie_val.rpartition(".")
    expect = hmac.new(bytes.fromhex(sec), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expect):
        return None
    try:
        pad = payload + "=" * (-len(payload) % 4)
        d = json.loads(base64.urlsafe_b64decode(pad))
    except (ValueError, json.JSONDecodeError):
        return None
    if int(d.get("x", 0)) < time.time():
        return None
    if d.get("e", "").lower() != a.get("email", "").lower():
        return None
    return d.get("e")
