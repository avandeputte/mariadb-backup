"""Where backups are kept: a local folder or Google Drive. Same five verbs for both."""

import datetime as _dt
import os
import re
import shutil

from mbm import gdrive
from mbm.base import CACHE_DIR
from mbm.engine import ARCHIVE_SUFFIX, EngineError

NAME_RX = re.compile(r"^(?P<label>.+)-(?P<stamp>\d{8}-\d{6})" + re.escape(ARCHIVE_SUFFIX) + "$")


def _entry(name, size, created, ref):
    m = NAME_RX.match(name)
    return {"name": name, "label": m.group("label") if m else None, "size": int(size or 0),
            "created": created, "ref": ref}


def _stamp_time(name):
    m = NAME_RX.match(name)
    if not m:
        return None
    try:
        return _dt.datetime.strptime(m.group("stamp"), "%Y%m%d-%H%M%S").isoformat()
    except ValueError:
        return None


def safe_name(name):
    if not NAME_RX.match(name or "") or "/" in name or "\\" in name or name.startswith("."):
        raise EngineError("Not a backup file name: %r" % name)
    return name


class Local:
    def __init__(self, dest):
        self.dest = dest
        self.path = dest.get("path") or ""
        if not os.path.isabs(self.path):
            raise EngineError("A local destination needs an absolute folder path")

    def test(self):
        os.makedirs(self.path, exist_ok=True)
        probe = os.path.join(self.path, ".mbm-write-test")
        with open(probe, "w") as f:
            f.write("ok")
        os.unlink(probe)
        st = shutil.disk_usage(self.path)
        return "Writable, %.1f GB free of %.1f GB" % (st.free / 1e9, st.total / 1e9)

    def list(self):
        if not os.path.isdir(self.path):
            return []
        out = []
        for n in os.listdir(self.path):
            if NAME_RX.match(n):
                p = os.path.join(self.path, n)
                out.append(_entry(n, os.path.getsize(p), _stamp_time(n), n))
        return sorted(out, key=lambda e: e["name"].rsplit("-", 2)[-2:], reverse=True)

    def store(self, tmp_path, log=None, cancel=None):
        os.makedirs(self.path, exist_ok=True)
        final = os.path.join(self.path, os.path.basename(tmp_path))
        shutil.move(tmp_path, final)
        os.chmod(final, 0o600)
        return final

    def fetch(self, name):
        """A local path to read the backup from."""
        p = os.path.join(self.path, safe_name(name))
        if not os.path.isfile(p):
            raise EngineError("%s is not in %s" % (name, self.path))
        return p

    def delete(self, name):
        p = os.path.join(self.path, safe_name(name))
        if os.path.isfile(p):
            os.unlink(p)

    def free_bytes(self):
        try:
            return shutil.disk_usage(self.path).free
        except OSError:
            return None


class GoogleDrive:
    def __init__(self, dest):
        self.dest = dest
        self.drive = gdrive.Drive(dest)

    def test(self):
        folder = self.drive.folder_id()
        who = self.drive.about()
        n = len([f for f in self.drive.list(folder) if NAME_RX.match(f["name"])])
        return "Connected%s; folder holds %d backup(s)" % (
            " as " + who["emailAddress"] if who.get("emailAddress") else "", n)

    def list(self):
        folder = self.drive.folder_id()
        out = [_entry(f["name"], f.get("size"), _stamp_time(f["name"]) or f.get("createdTime"), f["id"])
               for f in self.drive.list(folder) if NAME_RX.match(f["name"])]
        return sorted(out, key=lambda e: e["name"].rsplit("-", 2)[-2:], reverse=True)

    def store(self, tmp_path, log=None, cancel=None):
        folder = self.drive.folder_id()
        if log:
            log("Uploading to Google Drive")
        self.drive.upload(folder, tmp_path, log, cancel)
        os.unlink(tmp_path)
        return "gdrive:" + os.path.basename(tmp_path)

    def _file(self, name):
        f = self.drive.find(self.drive.folder_id(), safe_name(name))
        if not f:
            raise EngineError("%s is not in the Google Drive folder" % name)
        return f

    def fetch(self, name, log=None):
        """Download into the cache (once) and return the local path."""
        f = self._file(name)
        d = CACHE_DIR / self.dest["id"]
        d.mkdir(parents=True, exist_ok=True)
        p = d / name
        if p.exists() and p.stat().st_size == int(f.get("size") or -1):
            return str(p)
        # keep at most one cached download per destination
        for old in d.iterdir():
            old.unlink()
        if log:
            log("Downloading %s from Google Drive" % name)
        self.drive.download(f["id"], str(p))
        os.chmod(p, 0o600)
        return str(p)

    def stream(self, name):
        return self.drive.stream(self._file(name)["id"])

    def delete(self, name):
        f = self.drive.find(self.drive.folder_id(), safe_name(name))
        if f:
            self.drive.delete(f["id"])

    def free_bytes(self):
        return None


def open_destination(dest):
    if dest.get("type") == "local":
        return Local(dest)
    if dest.get("type") == "gdrive":
        return GoogleDrive(dest)
    raise EngineError("Unknown destination type %r" % dest.get("type"))


def prune(dest_obj, label, keep, log=None):
    """Keep the newest ``keep`` backups with this label; delete the rest."""
    if not keep or keep < 1:
        return []
    mine = [e for e in dest_obj.list() if e["label"] == label]
    mine.sort(key=lambda e: e["name"], reverse=True)
    removed = []
    for e in mine[keep:]:
        dest_obj.delete(e["name"])
        removed.append(e["name"])
        if log:
            log("Retention: removed %s" % e["name"])
    return removed
