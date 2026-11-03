"""Runs (backups and restores), the queue that executes them one at a time,
and the scheduler that starts jobs on their cron schedule.

Everything runs in one worker thread, in order. Two dumps of the same server
at once would only compete for its I/O, and a restore must never overlap a
backup of the database it is writing.
"""

import datetime as _dt
import json
import os
import queue
import shutil
import threading
import time
import traceback

from mbm import engine
from mbm.base import (RUNS_PATH, WORK_DIR, _write_json, find, load_config, log, new_id)
from mbm.destinations import open_destination, prune

MAX_RUNS = 300
MAX_LOG_LINES = 2000

_runs_lock = threading.RLock()
_runs = {"list": None}
_queue = queue.Queue()
_cancel = {}  # run id -> Event


def _now():
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _load_runs():
    if _runs["list"] is None:
        try:
            with open(RUNS_PATH) as f:
                _runs["list"] = json.load(f)
        except (OSError, ValueError):
            _runs["list"] = []
        # anything that was mid-flight when the process stopped did not finish
        for r in _runs["list"]:
            if r["status"] in ("queued", "running"):
                r["status"] = "failed"
                r.setdefault("log", []).append("%s interrupted: the service stopped" % _now())
                r["finished"] = r.get("finished") or _now()
    return _runs["list"]


def _save_runs():
    _write_json(RUNS_PATH, _runs["list"][-MAX_RUNS:])


def list_runs(limit=100, job_id=None):
    with _runs_lock:
        rs = _load_runs()
        if job_id:
            rs = [r for r in rs if r.get("job_id") == job_id]
        out = []
        for r in reversed(rs[-limit:] if not job_id else rs):
            c = {k: v for k, v in r.items() if k != "log"}
            c["last"] = r["log"][-1] if r.get("log") else ""
            out.append(c)
        return out[:limit]


def get_run(run_id):
    with _runs_lock:
        for r in _load_runs():
            if r["id"] == run_id:
                return json.loads(json.dumps(r))
    return None


def _update(run_id, **fields):
    with _runs_lock:
        for r in _load_runs():
            if r["id"] == run_id:
                r.update(fields)
                _save_runs()
                return r


def _logger(run_id):
    def emit(msg):
        line = "%s %s" % (_dt.datetime.now().strftime("%H:%M:%S"), msg)
        with _runs_lock:
            for r in _load_runs():
                if r["id"] == run_id:
                    r.setdefault("log", []).append(line)
                    if len(r["log"]) > MAX_LOG_LINES:
                        r["log"] = r["log"][:50] + ["... (log trimmed) ..."] + r["log"][-(MAX_LOG_LINES - 60):]
                    break
            _save_runs()
        log.info("[%s] %s", run_id, msg)
    return emit


def enqueue(kind, title, payload, job_id=None, trigger="manual"):
    run = {"id": new_id("run"), "kind": kind, "title": title, "job_id": job_id,
           "trigger": trigger, "status": "queued", "queued": _now(), "started": None,
           "finished": None, "log": [], "result": None, "payload": payload}
    with _runs_lock:
        rs = _load_runs()
        rs.append(run)
        del rs[:-MAX_RUNS]
        _save_runs()
    _cancel[run["id"]] = threading.Event()
    _queue.put(run["id"])
    return run["id"]


def cancel(run_id):
    ev = _cancel.get(run_id)
    r = get_run(run_id)
    if not r or r["status"] not in ("queued", "running") or ev is None:
        return False
    ev.set()
    if r["status"] == "queued":
        _update(run_id, status="cancelled", finished=_now())
    return True


def busy():
    with _runs_lock:
        return any(r["status"] in ("queued", "running") for r in _load_runs())


def job_is_pending(job_id):
    with _runs_lock:
        return any(r.get("job_id") == job_id and r["status"] in ("queued", "running") for r in _load_runs())


# --------------------------------------------------------------------------
# what a run does
# --------------------------------------------------------------------------

def _do_backup(run, emit, ev):
    cfg = load_config()
    p = run["payload"]
    job = find(cfg["jobs"], p["job_id"])
    if not job:
        raise engine.EngineError("The job no longer exists")
    server = find(cfg["servers"], job["server_id"])
    dest = find(cfg["destinations"], job["destination_id"])
    if not server or not dest:
        raise engine.EngineError("The job's server or destination no longer exists")
    label = engine.slug(job["name"])
    emit("Backing up %s to %s" % (server["name"], dest["name"]))
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    tar, manifest = engine.run_backup(
        server, job.get("databases") or "all", include_users=job.get("include_users", True),
        workdir=str(WORK_DIR), label=label, lock_all=job.get("lock_all", False),
        desync=job.get("desync", False), compress_level=int(job.get("compress_level") or 6),
        log=emit, cancel=ev, source_name=server["name"])
    try:
        size = os.path.getsize(tar)
        d = open_destination(dest)
        where = d.store(tar, emit, ev)
        emit("Stored as %s" % where)
    finally:
        if os.path.exists(tar):
            os.unlink(tar)
    removed = []
    keep = int(job.get("keep") or 0)
    if keep:
        try:
            removed = prune(d, label, keep, emit)
            if not removed:
                emit("Retention: %d or fewer backups kept, nothing to remove" % keep)
        except Exception as e:  # a failed prune must not mark a good backup failed
            emit("warning: retention clean-up failed: %s" % e)
    return {"file": os.path.basename(tar), "size": size, "destination_id": dest["id"],
            "databases": [d["name"] for d in manifest["databases"]], "pruned": removed}


def _do_restore(run, emit, ev):
    cfg = load_config()
    p = run["payload"]
    dest = find(cfg["destinations"], p["destination_id"])
    server = find(cfg["servers"], p["server_id"])
    if not dest or not server:
        raise engine.EngineError("The destination or target server no longer exists")
    d = open_destination(dest)
    path = d.fetch(p["file"], emit) if dest["type"] == "gdrive" else d.fetch(p["file"])
    emit("Restoring %s onto %s" % (p["file"], server["name"]))
    with engine.Archive(path) as a:
        errors = engine.run_restore(server, a, p["plan"], log=emit, cancel=ev)
    if errors:
        raise engine.EngineError("%d error(s) during restore; see the log" % len(errors))
    return {"file": p["file"], "server_id": server["id"]}


def _worker():
    while True:
        run_id = _queue.get()
        run = get_run(run_id)
        if not run or run["status"] != "queued":
            continue
        ev = _cancel.setdefault(run_id, threading.Event())
        emit = _logger(run_id)
        _update(run_id, status="running", started=_now())
        t0 = time.time()
        try:
            fn = _do_backup if run["kind"] == "backup" else _do_restore
            result = fn(run, emit, ev)
            emit("Finished in %s" % _dur(time.time() - t0))
            _update(run_id, status="ok", finished=_now(), result=result)
        except engine.Cancelled:
            emit("Cancelled")
            _update(run_id, status="cancelled", finished=_now())
        except Exception as e:
            if ev.is_set():
                emit("Cancelled")
                _update(run_id, status="cancelled", finished=_now())
            else:
                emit("FAILED: %s" % e)
                if not isinstance(e, engine.EngineError):
                    log.error("run %s crashed:\n%s", run_id, traceback.format_exc())
                _update(run_id, status="failed", finished=_now())
        finally:
            _cancel.pop(run_id, None)
            shutil.rmtree(WORK_DIR, ignore_errors=True) if not busy() else None


def _dur(s):
    s = int(s)
    return "%dm %02ds" % (s // 60, s % 60) if s >= 60 else "%ds" % s


# --------------------------------------------------------------------------
# cron
# --------------------------------------------------------------------------

def _field(spec, lo, hi):
    vals = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, st = part.split("/", 1)
            step = int(st)
            if step < 1:
                raise ValueError("bad step")
        if part in ("*", ""):
            a, b = lo, hi
        elif "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
        else:
            a = b = int(part)
            if step > 1:
                b = hi
        if a < lo or b > hi or a > b:
            raise ValueError("%s out of range %d-%d" % (part, lo, hi))
        vals.update(range(a, b + 1, step))
    return vals


class Cron:
    """Five-field cron: minute hour day-of-month month day-of-week (0 or 7 = Sunday)."""

    def __init__(self, expr):
        parts = (expr or "").split()
        if len(parts) != 5:
            raise ValueError("a cron expression has five fields")
        self.minutes = _field(parts[0], 0, 59)
        self.hours = _field(parts[1], 0, 23)
        self.dom = _field(parts[2], 1, 31)
        self.months = _field(parts[3], 1, 12)
        dow = _field(parts[4], 0, 7)
        self.dow = {0 if d == 7 else d for d in dow}
        self.dom_any, self.dow_any = parts[2] == "*", parts[4] == "*"

    def _day_ok(self, d):
        if d.month not in self.months:
            return False
        cron_dow = (d.weekday() + 1) % 7  # Python Monday=0 -> cron Monday=1
        in_dom, in_dow = d.day in self.dom, cron_dow in self.dow
        if self.dom_any and self.dow_any:
            return True
        if self.dom_any:
            return in_dow
        if self.dow_any:
            return in_dom
        return in_dom or in_dow  # classic cron: either restriction matches

    def matches(self, t):
        return t.minute in self.minutes and t.hour in self.hours and self._day_ok(t)

    def next_after(self, t):
        t = t.replace(second=0, microsecond=0) + _dt.timedelta(minutes=1)
        for _ in range(366 * 2):
            if self._day_ok(t):
                for h in sorted(self.hours):
                    if h < t.hour:
                        continue
                    for m in sorted(self.minutes):
                        if h == t.hour and m < t.minute:
                            continue
                        return t.replace(hour=h, minute=m)
            t = (t + _dt.timedelta(days=1)).replace(hour=0, minute=0)
        return None


def schedule_to_cron(s):
    """The UI's schedule form -> a cron expression ('' means run only by hand)."""
    s = s or {}
    mode = s.get("mode") or "manual"
    if mode == "manual":
        return ""
    if mode == "cron":
        Cron(s.get("cron") or "")
        return s["cron"].strip()
    if mode == "hourly":
        every = int(s.get("every") or 1)
        return "%d */%d * * *" % (int(s.get("minute") or 0), every) if every > 1 else "%d * * * *" % int(s.get("minute") or 0)
    hh, mm = (int(x) for x in (s.get("time") or "02:00").split(":"))
    if mode == "daily":
        return "%d %d * * *" % (mm, hh)
    if mode == "weekly":
        days = sorted({int(d) % 7 for d in (s.get("days") or [0])})
        return "%d %d * * %s" % (mm, hh, ",".join(str(d) for d in days))
    if mode == "monthly":
        return "%d %d %d * *" % (mm, hh, int(s.get("day") or 1))
    raise ValueError("unknown schedule mode %r" % mode)


def next_run(job):
    if not job.get("enabled", True):
        return None
    expr = job.get("cron")
    if not expr:
        return None
    try:
        n = Cron(expr).next_after(_dt.datetime.now())
    except ValueError:
        return None
    return n.astimezone().isoformat(timespec="seconds") if n else None


def _scheduler():
    last = _dt.datetime.now().replace(second=0, microsecond=0)
    while True:
        time.sleep(15)
        now = _dt.datetime.now().replace(second=0, microsecond=0)
        if now <= last:
            if now < last:  # clock went backwards; resync without firing
                last = now
            continue
        # every minute since the last look (bounded, so a suspended host does
        # not fire a day's worth of jobs when it wakes)
        minutes = []
        t = last + _dt.timedelta(minutes=1)
        while t <= now and len(minutes) < 90:
            minutes.append(t)
            t += _dt.timedelta(minutes=1)
        last = now
        try:
            cfg = load_config()
        except Exception:
            continue
        for job in cfg.get("jobs", []):
            if not job.get("enabled", True) or not job.get("cron"):
                continue
            try:
                c = Cron(job["cron"])
            except ValueError:
                continue
            if any(c.matches(m) for m in minutes):
                if job_is_pending(job["id"]):
                    log.info("job %s still running; skipping this scheduled start", job["name"])
                    continue
                enqueue("backup", "Backup: %s" % job["name"], {"job_id": job["id"]},
                        job_id=job["id"], trigger="schedule")


def start_background():
    with _runs_lock:
        _load_runs()
        _save_runs()
    threading.Thread(target=_worker, name="mbm-worker", daemon=True).start()
    threading.Thread(target=_scheduler, name="mbm-scheduler", daemon=True).start()
