"""The JSON API the web UI talks to, and the static files it is made of."""

import datetime as _dt
import os
import threading
import time

from flask import Response, jsonify, request, send_file, send_from_directory, stream_with_context

from mbm import engine, gdrive, tasks
from mbm.base import (DEFAULT_LOCAL_DIR, STATIC_DIR, VERSION, app, find, load_config, new_id,
                      update_config)
from mbm.destinations import open_destination, safe_name

SECRET_FIELDS = {"servers": ["password"], "destinations": ["client_secret", "refresh_token", "sa_json"]}


class ApiError(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


@app.errorhandler(ApiError)
def _api_error(e):
    return jsonify(error=str(e)), e.status


@app.errorhandler(engine.EngineError)
def _engine_error(e):
    return jsonify(error=str(e)), 400


@app.errorhandler(gdrive.DriveError)
def _drive_error(e):
    return jsonify(error=str(e)), 502


def _body():
    return request.get_json(silent=True) or {}


def _public(kind, item):
    out = dict(item)
    for f in SECRET_FIELDS.get(kind, []):
        out["has_" + f] = bool(out.get(f))
        out.pop(f, None)
    if kind == "destinations" and item.get("type") == "gdrive":
        out["signed_in"] = bool(item.get("refresh_token")) or (
            item.get("auth") == "service_account" and bool(item.get("sa_json")))
    return out


def _merge_secrets(kind, new, old):
    """A secret left out (or null) in an edit keeps the stored value."""
    for f in SECRET_FIELDS.get(kind, []):
        if new.get(f) in (None,) and old:
            new[f] = old.get(f)
    return new


# --------------------------------------------------------------------------
# pages
# --------------------------------------------------------------------------

@app.get("/")
def index():
    html = (STATIC_DIR / "index.html").read_text().replace("__VERSION__", VERSION)
    return Response(html, mimetype="text/html", headers={"Cache-Control": "no-store"})


@app.get("/static/v/<ver>/<path:name>")
def static_versioned(ver, name):
    resp = send_from_directory(STATIC_DIR, name)
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


@app.get("/static/<path:name>")
def static_files(name):
    return send_from_directory(STATIC_DIR, name)


@app.after_request
def _headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

@app.get("/api/state")
def api_state():
    cfg = load_config()
    runs = tasks.list_runs(300)
    last = {}
    for r in runs:  # newest first
        if r.get("job_id") and r["status"] not in ("queued", "running") and r["job_id"] not in last:
            last[r["job_id"]] = {"id": r["id"], "status": r["status"], "finished": r["finished"],
                                 "result": r.get("result")}
    jobs = []
    for j in cfg["jobs"]:
        jj = dict(j)
        jj["next_run"] = tasks.next_run(j)
        jj["last_run"] = last.get(j["id"])
        jj["pending"] = tasks.job_is_pending(j["id"])
        jobs.append(jj)
    return jsonify(
        version=VERSION, user=cfg["admin"].get("username"),
        now=_dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        servers=[_public("servers", s) for s in cfg["servers"]],
        destinations=[_public("destinations", d) for d in cfg["destinations"]],
        jobs=jobs, default_local_dir=DEFAULT_LOCAL_DIR,
        busy=tasks.busy())


# --------------------------------------------------------------------------
# servers
# --------------------------------------------------------------------------

def _clean_server(b):
    name = (b.get("name") or "").strip()
    if not name:
        raise ApiError("Give the server a name")
    port = b.get("port")
    if port in ("", None):
        port = None
    else:
        try:
            port = int(port)
        except (TypeError, ValueError):
            raise ApiError("The port must be a number")
    return {"name": name, "host": (b.get("host") or "").strip(), "port": port,
            "socket": (b.get("socket") or "").strip(), "user": (b.get("user") or "").strip(),
            "password": b.get("password"), "ssl": bool(b.get("ssl"))}


@app.post("/api/servers")
def api_server_save():
    b = _body()
    s = _clean_server(b)

    def apply(cfg):
        if b.get("id"):
            old = find(cfg["servers"], b["id"])
            if not old:
                raise ApiError("No such server", 404)
            _merge_secrets("servers", s, old)
            old.update(s)
            return old["id"]
        s["id"] = new_id("srv")
        s["password"] = s.get("password") or ""
        cfg["servers"].append(s)
        return s["id"]
    return jsonify(id=update_config(apply))


@app.delete("/api/servers/<sid>")
def api_server_delete(sid):
    def apply(cfg):
        users = [j["name"] for j in cfg["jobs"] if j.get("server_id") == sid]
        if users:
            raise ApiError("Used by job(s): %s" % ", ".join(users))
        cfg["servers"] = [s for s in cfg["servers"] if s["id"] != sid]
    update_config(apply)
    return jsonify(ok=True)


@app.post("/api/servers/test")
def api_server_test():
    b = _body()
    s = _clean_server(dict(b, name=b.get("name") or "test"))
    if b.get("id"):
        _merge_secrets("servers", s, find(load_config()["servers"], b["id"]))
    with engine.Connection(s) as conn:
        info = engine.server_info(conn)
    return jsonify(info)


@app.get("/api/servers/<sid>/info")
def api_server_info(sid):
    s = find(load_config()["servers"], sid)
    if not s:
        raise ApiError("No such server", 404)
    with engine.Connection(s) as conn:
        return jsonify(engine.server_info(conn))


# --------------------------------------------------------------------------
# destinations
# --------------------------------------------------------------------------

def _clean_dest(b):
    name = (b.get("name") or "").strip()
    if not name:
        raise ApiError("Give the destination a name")
    t = b.get("type")
    if t == "local":
        path = (b.get("path") or "").strip()
        if not path.startswith("/"):
            raise ApiError("The folder must be an absolute path, like /var/backups/mariadb")
        return {"name": name, "type": "local", "path": os.path.normpath(path)}
    if t == "gdrive":
        auth = b.get("auth") or "oauth"
        d = {"name": name, "type": "gdrive", "auth": auth,
             "folder_name": (b.get("folder_name") or "MariaDB Backups").strip(),
             "folder_id": (b.get("folder_id") or "").strip(),
             "client_id": (b.get("client_id") or "").strip(),
             "client_secret": b.get("client_secret"), "sa_json": b.get("sa_json"),
             "refresh_token": None, "account": b.get("account")}
        if auth == "service_account" and not d["folder_id"]:
            raise ApiError("A service account needs the ID of a Shared Drive folder")
        return d
    raise ApiError("Pick local folder or Google Drive")


@app.post("/api/destinations")
def api_dest_save():
    b = _body()
    d = _clean_dest(b)

    def apply(cfg):
        if b.get("id"):
            old = find(cfg["destinations"], b["id"])
            if not old:
                raise ApiError("No such destination", 404)
            _merge_secrets("destinations", d, old)
            if d["type"] == "gdrive":
                # a different Google client or folder means signing in again
                if old.get("client_id") != d.get("client_id") or old.get("auth") != d.get("auth"):
                    d["refresh_token"] = None
                    d["account"] = None
                else:
                    d["account"] = old.get("account")
            old.clear()
            old.update(d, id=b["id"])
            gdrive.forget_token(b["id"])
            return b["id"]
        d["id"] = new_id("dst")
        cfg["destinations"].append(d)
        return d["id"]
    return jsonify(id=update_config(apply))


@app.delete("/api/destinations/<did>")
def api_dest_delete(did):
    def apply(cfg):
        users = [j["name"] for j in cfg["jobs"] if j.get("destination_id") == did]
        if users:
            raise ApiError("Used by job(s): %s" % ", ".join(users))
        cfg["destinations"] = [d for d in cfg["destinations"] if d["id"] != did]
    update_config(apply)
    return jsonify(ok=True)


@app.post("/api/destinations/<did>/test")
def api_dest_test(did):
    d = find(load_config()["destinations"], did)
    if not d:
        raise ApiError("No such destination", 404)
    return jsonify(message=open_destination(d).test())


_flows = {}


@app.post("/api/destinations/<did>/google/start")
def api_google_start(did):
    d = find(load_config()["destinations"], did)
    if not d or d.get("type") != "gdrive":
        raise ApiError("No such Google Drive destination", 404)
    if not d.get("client_id") or not d.get("client_secret"):
        raise ApiError("Save the OAuth client ID and secret first")
    info = gdrive.start_device_flow(d["client_id"])
    flow = {"id": new_id("flow"), "dest": did, "status": "pending", "error": None,
            "user_code": info["user_code"],
            "url": info.get("verification_url") or info.get("verification_uri"),
            "expires": time.time() + int(info.get("expires_in", 1800))}
    _flows[flow["id"]] = flow

    def poll():
        interval = int(info.get("interval", 5))
        while time.time() < flow["expires"] and flow["status"] == "pending":
            time.sleep(interval)
            try:
                st, data = gdrive.poll_device_flow(d["client_id"], d["client_secret"], info["device_code"])
            except Exception as e:
                flow.update(status="error", error=str(e))
                return
            if st == "slow_down":
                interval += 5
            elif st == "ok":
                def save(cfg):
                    dd = find(cfg["destinations"], did)
                    if dd:
                        dd["refresh_token"] = data.get("refresh_token") or dd.get("refresh_token")
                update_config(save)
                gdrive.forget_token(did)
                try:
                    dd = find(load_config()["destinations"], did)
                    who = gdrive.Drive(dd).about().get("emailAddress")
                    update_config(lambda cfg: find(cfg["destinations"], did).update(account=who))
                except Exception:
                    pass
                flow["status"] = "ok"
                return
            elif st in ("denied", "expired"):
                flow["status"] = st
                return
        if flow["status"] == "pending":
            flow["status"] = "expired"

    threading.Thread(target=poll, daemon=True).start()
    return jsonify({k: flow[k] for k in ("id", "user_code", "url", "status")})


@app.get("/api/google/flows/<fid>")
def api_google_flow(fid):
    f = _flows.get(fid)
    if not f:
        raise ApiError("No such sign-in", 404)
    return jsonify(status=f["status"], error=f["error"])


# --------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------

@app.post("/api/jobs")
def api_job_save():
    b = _body()
    name = (b.get("name") or "").strip()
    if not name:
        raise ApiError("Give the job a name")
    dbs = b.get("databases")
    if dbs != "all":
        if not isinstance(dbs, list) or not all(isinstance(x, str) for x in dbs):
            raise ApiError("Pick the databases, or All")
        if not dbs and not b.get("include_users", True):
            raise ApiError("Pick at least one database, or include users")
    try:
        keep = int(b.get("keep") or 0)
        level = int(b.get("compress_level") or 6)
    except (TypeError, ValueError):
        raise ApiError("Keep and compression must be numbers")
    if keep < 0 or keep > 10000:
        raise ApiError("Keep must be between 0 (keep all) and 10000")
    try:
        cron = tasks.schedule_to_cron(b.get("schedule"))
    except (ValueError, TypeError) as e:
        raise ApiError("Schedule: %s" % e)
    job = {"name": name, "server_id": b.get("server_id"), "destination_id": b.get("destination_id"),
           "databases": dbs, "include_users": bool(b.get("include_users", True)), "keep": keep,
           "schedule": b.get("schedule") or {"mode": "manual"}, "cron": cron,
           "enabled": bool(b.get("enabled", True)), "desync": bool(b.get("desync")),
           "lock_all": bool(b.get("lock_all")), "compress_level": max(1, min(9, level))}

    def apply(cfg):
        if not find(cfg["servers"], job["server_id"]):
            raise ApiError("Pick a server")
        if not find(cfg["destinations"], job["destination_id"]):
            raise ApiError("Pick a destination")
        label = engine.slug(name)
        for j in cfg["jobs"]:
            if j["id"] != b.get("id") and engine.slug(j["name"]) == label:
                raise ApiError("Another job is already called %r; names must differ because "
                               "retention tells backups apart by them" % j["name"])
        if b.get("id"):
            old = find(cfg["jobs"], b["id"])
            if not old:
                raise ApiError("No such job", 404)
            old.update(job)
            return old["id"]
        job["id"] = new_id("job")
        cfg["jobs"].append(job)
        return job["id"]
    return jsonify(id=update_config(apply))


@app.delete("/api/jobs/<jid>")
def api_job_delete(jid):
    update_config(lambda cfg: cfg.__setitem__("jobs", [j for j in cfg["jobs"] if j["id"] != jid]))
    return jsonify(ok=True)


@app.post("/api/jobs/<jid>/run")
def api_job_run(jid):
    j = find(load_config()["jobs"], jid)
    if not j:
        raise ApiError("No such job", 404)
    if tasks.job_is_pending(jid):
        raise ApiError("This job is already queued or running")
    rid = tasks.enqueue("backup", "Backup: %s" % j["name"], {"job_id": jid}, job_id=jid)
    return jsonify(run_id=rid)


# --------------------------------------------------------------------------
# backups and restore
# --------------------------------------------------------------------------

def _dest(did):
    d = find(load_config()["destinations"], did)
    if not d:
        raise ApiError("No such destination", 404)
    return d, open_destination(d)


@app.get("/api/backups/<did>")
def api_backups(did):
    d, obj = _dest(did)
    items = obj.list()
    return jsonify(backups=items, free=obj.free_bytes())


def _archive_path(did, name):
    d, obj = _dest(did)
    safe_name(name)
    return obj.fetch(name)


@app.get("/api/backups/<did>/<name>/contents")
def api_backup_contents(did, name):
    with engine.Archive(_archive_path(did, name)) as a:
        return jsonify(a.contents())


@app.post("/api/backups/<did>/<name>/verify")
def api_backup_verify(did, name):
    with engine.Archive(_archive_path(did, name)) as a:
        n, problems = a.verify()
    return jsonify(files=n, problems=problems)


@app.post("/api/backups/<did>/<name>/preview")
def api_restore_preview(did, name):
    b = _body()
    server = find(load_config()["servers"], b.get("server_id"))
    if not server:
        raise ApiError("Pick the server to restore onto")
    with engine.Archive(_archive_path(did, name)) as a:
        norm = engine.normalize_plan(a, b.get("plan") or {})
        lines = engine.describe(norm)
        with engine.Connection(server) as conn:
            warnings = engine.preflight(conn, norm)
            info = engine.server_info(conn)
    return jsonify(lines=lines, warnings=warnings, galera=info["galera"], version=info["version"])


@app.post("/api/backups/<did>/<name>/restore")
def api_restore(did, name):
    b = _body()
    cfg = load_config()
    server = find(cfg["servers"], b.get("server_id"))
    if not server:
        raise ApiError("Pick the server to restore onto")
    with engine.Archive(_archive_path(did, name)) as a:
        engine.normalize_plan(a, b.get("plan") or {})
    rid = tasks.enqueue("restore", "Restore: %s onto %s" % (name, server["name"]),
                        {"destination_id": did, "file": name, "server_id": server["id"],
                         "plan": b.get("plan")})
    return jsonify(run_id=rid)


@app.delete("/api/backups/<did>/<name>")
def api_backup_delete(did, name):
    d, obj = _dest(did)
    obj.delete(safe_name(name))
    return jsonify(ok=True)


@app.get("/api/backups/<did>/<name>/download")
def api_backup_download(did, name):
    d, obj = _dest(did)
    safe_name(name)
    if d["type"] == "local":
        return send_file(obj.fetch(name), as_attachment=True, download_name=name,
                         mimetype="application/x-tar")
    r = obj.stream(name)
    headers = {"Content-Disposition": 'attachment; filename="%s"' % name}
    if r.headers.get("Content-Length"):
        headers["Content-Length"] = r.headers["Content-Length"]
    return Response(stream_with_context(r.iter_content(1 << 20)), mimetype="application/x-tar",
                    headers=headers)


# --------------------------------------------------------------------------
# runs
# --------------------------------------------------------------------------

@app.get("/api/runs")
def api_runs():
    return jsonify(runs=tasks.list_runs(int(request.args.get("limit", 100)), request.args.get("job")))


@app.get("/api/runs/<rid>")
def api_run(rid):
    r = tasks.get_run(rid)
    if not r:
        raise ApiError("No such run", 404)
    r.pop("payload", None)
    return jsonify(r)


@app.post("/api/runs/<rid>/cancel")
def api_run_cancel(rid):
    if not tasks.cancel(rid):
        raise ApiError("That run is not queued or running")
    return jsonify(ok=True)
