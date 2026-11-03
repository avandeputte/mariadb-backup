"""Shared foundation: paths, the JSON config store, logging, the Flask app."""

import copy
import json
import logging
import logging.handlers
import os
import secrets
import threading
from pathlib import Path

from flask import Flask

DATA_DIR = Path(os.environ.get("MBM_DATA_DIR", "/var/lib/mariadb-backup-manager"))
CONF_PATH = DATA_DIR / "config.json"
RUNS_PATH = DATA_DIR / "runs.json"
WORK_DIR = Path(os.environ.get("MBM_WORK_DIR", str(DATA_DIR / "work")))
CACHE_DIR = Path(os.environ.get("MBM_CACHE_DIR", str(DATA_DIR / "cache")))
DEFAULT_LOCAL_DIR = os.environ.get("MBM_DEFAULT_BACKUP_DIR", "/var/backups/mariadb")
LISTEN = os.environ.get("MBM_LISTEN", "0.0.0.0")
PORT = int(os.environ.get("MBM_PORT", "8090"))
THREADS = int(os.environ.get("MBM_THREADS", "8"))
ROOT_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT_DIR / "static"


def _read_version():
    try:
        return (ROOT_DIR / "VERSION").read_text().strip() or "0"
    except OSError:
        return "0"


VERSION = _read_version()

log = logging.getLogger("mbm")


def setup_logging():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    log.setLevel(logging.INFO)
    if not log.handlers:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.addHandler(sh)
        try:
            fh = logging.handlers.RotatingFileHandler(DATA_DIR / "mbm.log", maxBytes=2 << 20, backupCount=2)
            fh.setFormatter(fmt)
            log.addHandler(fh)
        except OSError:
            pass


app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 1 << 20

# --------------------------------------------------------------------------
# config store: one JSON file, written atomically, 0600 (it holds passwords)
# --------------------------------------------------------------------------

_lock = threading.RLock()
_cache = {"cfg": None}

DEFAULTS = {
    "admin": {},
    "secret": "",
    "servers": [],
    "destinations": [],
    "jobs": [],
}


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def load_config():
    with _lock:
        if _cache["cfg"] is None:
            cfg = copy.deepcopy(DEFAULTS)
            if CONF_PATH.exists():
                with open(CONF_PATH) as f:
                    cfg.update(json.load(f))
            if not cfg.get("secret"):
                cfg["secret"] = secrets.token_hex(32)
                _write_json(CONF_PATH, cfg)
            _cache["cfg"] = cfg
        return copy.deepcopy(_cache["cfg"])


def save_config(cfg):
    with _lock:
        _write_json(CONF_PATH, cfg)
        _cache["cfg"] = copy.deepcopy(cfg)


def update_config(fn):
    """Load, let fn change it, save -- all under the lock. Returns fn's result."""
    with _lock:
        cfg = load_config()
        result = fn(cfg)
        save_config(cfg)
        return result


def new_id(prefix):
    return prefix + "_" + secrets.token_hex(4)


def find(items, item_id):
    for it in items:
        if it.get("id") == item_id:
            return it
    return None
