"""The single administrator login, sessions, and the guard on every API call."""

import hashlib
import hmac
import os
import secrets
import time

from flask import jsonify, request

from mbm.base import app, load_config, update_config

PBKDF2_ITERATIONS = 310000
SESSION_COOKIE = "mbm_session"
SESSION_HOURS = 12
_fails = {}


def hash_password(password, salt=None, iterations=PBKDF2_ITERATIONS):
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), iterations)
    return {"salt": salt, "hash": dk.hex(), "iterations": iterations}


def verify_password(password, admin):
    if not (admin.get("hash") and admin.get("salt")):
        return False
    got = hash_password(password, admin["salt"], admin.get("iterations") or PBKDF2_ITERATIONS)
    return hmac.compare_digest(got["hash"], admin["hash"])


def set_admin(cfg, username, password):
    rec = hash_password(password)
    rec["username"] = username
    # a new password invalidates every existing session
    rec["epoch"] = secrets.token_hex(4)
    cfg["admin"] = rec


def _sign(cfg, payload):
    return hmac.new(cfg["secret"].encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_session(cfg):
    exp = int(time.time()) + SESSION_HOURS * 3600
    payload = "%s|%d|%s" % (cfg["admin"]["username"], exp, cfg["admin"].get("epoch", ""))
    return payload + "|" + _sign(cfg, payload)


def read_session(cfg, token):
    try:
        user, exp, epoch, sig = (token or "").rsplit("|", 3)
    except ValueError:
        return None
    payload = "%s|%s|%s" % (user, exp, epoch)
    if not hmac.compare_digest(sig, _sign(cfg, payload)):
        return None
    admin = cfg.get("admin") or {}
    if int(exp) < time.time() or user != admin.get("username") or epoch != admin.get("epoch", ""):
        return None
    return user


def needs_setup(cfg):
    return not (cfg.get("admin") or {}).get("hash")


def _set_cookie(resp, value, max_age):
    resp.set_cookie(SESSION_COOKIE, value, max_age=max_age, httponly=True, samesite="Strict",
                    secure=request.is_secure)
    return resp


OPEN_PATHS = {"/api/whoami", "/api/login", "/api/setup"}


@app.before_request
def _guard():
    path = request.path
    if not path.startswith("/api/"):
        return None
    # State-changing calls must come from our own page: a cross-site form
    # cannot add a custom header, and SameSite=Strict keeps the cookie home.
    if request.method not in ("GET", "HEAD") and request.headers.get("X-MBM") != "1":
        return jsonify(error="missing X-MBM header"), 400
    if path in OPEN_PATHS:
        return None
    cfg = load_config()
    if read_session(cfg, request.cookies.get(SESSION_COOKIE)) is None:
        return jsonify(error="sign in first"), 401
    return None


@app.get("/api/whoami")
def api_whoami():
    from mbm.base import VERSION
    cfg = load_config()
    return jsonify(user=read_session(cfg, request.cookies.get(SESSION_COOKIE)),
                   setup=needs_setup(cfg), version=VERSION)


def _check_new(username, password):
    if not username or len(username) > 64:
        return "Pick a username"
    if len(password or "") < 8:
        return "The password must be at least 8 characters"
    return None


@app.post("/api/setup")
def api_setup():
    body = request.get_json(silent=True) or {}
    username, password = (body.get("username") or "").strip(), body.get("password") or ""
    bad = _check_new(username, password)
    if bad:
        return jsonify(error=bad), 400

    def apply(cfg):
        if not needs_setup(cfg):
            return False
        set_admin(cfg, username, password)
        return True
    if not update_config(apply):
        return jsonify(error="An administrator already exists"), 409
    cfg = load_config()
    return _set_cookie(jsonify(ok=True, user=username), make_session(cfg), SESSION_HOURS * 3600)


@app.post("/api/login")
def api_login():
    body = request.get_json(silent=True) or {}
    ip = request.remote_addr or "?"
    now = time.time()
    recent = [t for t in _fails.get(ip, []) if now - t < 900]
    if len(recent) >= 10:
        return jsonify(error="Too many failed sign-ins; wait a few minutes"), 429
    cfg = load_config()
    admin = cfg.get("admin") or {}
    if (body.get("username") or "") == admin.get("username") and verify_password(body.get("password") or "", admin):
        _fails.pop(ip, None)
        return _set_cookie(jsonify(ok=True, user=admin["username"]), make_session(cfg), SESSION_HOURS * 3600)
    recent.append(now)
    _fails[ip] = recent
    time.sleep(0.5)
    return jsonify(error="Wrong username or password"), 401


@app.post("/api/logout")
def api_logout():
    return _set_cookie(jsonify(ok=True), "", 0)


@app.post("/api/password")
def api_password():
    body = request.get_json(silent=True) or {}
    cfg = load_config()
    if not verify_password(body.get("current") or "", cfg.get("admin") or {}):
        return jsonify(error="The current password is wrong"), 400
    username = (body.get("username") or cfg["admin"]["username"]).strip()
    bad = _check_new(username, body.get("password"))
    if bad:
        return jsonify(error=bad), 400
    update_config(lambda c: set_admin(c, username, body["password"]))
    cfg = load_config()
    return _set_cookie(jsonify(ok=True, user=username), make_session(cfg), SESSION_HOURS * 3600)


def seed_admin_from_env():
    """Docker: MBM_ADMIN_USER / MBM_ADMIN_PASSWORD create the login on first start."""
    user, pw = os.environ.get("MBM_ADMIN_USER") or "admin", os.environ.get("MBM_ADMIN_PASSWORD")
    if not pw:
        return
    update_config(lambda c: set_admin(c, user, pw) if needs_setup(c) else None)
