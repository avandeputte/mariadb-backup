"""Google Drive storage against a fake Drive API (no network).

Covers folder find-or-create, chunked resumable upload including a failed
chunk that is resumed from the offset Google reports, listing, download,
delete and retention.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("MBM_DATA_DIR", tempfile.mkdtemp())

from mbm import gdrive  # noqa: E402
from mbm import destinations  # noqa: E402


class Resp:
    def __init__(self, status, json_data=None, headers=None, content=b""):
        self.status_code, self._json, self.headers, self.content = status, json_data, headers or {}, content
        self.text = str(json_data)

    def json(self):
        return self._json

    def iter_content(self, n):
        for i in range(0, len(self.content), n):
            yield self.content[i:i + n]


class FakeDrive:
    """Just enough of Drive v3 for the code under test."""

    def __init__(self):
        self.files = {}  # id -> dict(name, parents, mime, data)
        self.sessions = {}
        self.n = 0
        self.fail_next_put = False
        self.RequestException = Exception

    def _id(self):
        self.n += 1
        return "f%d" % self.n

    def post(self, url, data=None, json=None, headers=None, params=None, timeout=None):
        if url == gdrive.TOKEN_URL:
            return Resp(200, {"access_token": "tok", "expires_in": 3600})
        if url == gdrive.API + "/files":
            fid = self._id()
            self.files[fid] = {"name": json["name"], "parents": json.get("parents", []), "mime": json["mimeType"], "data": b""}
            return Resp(200, {"id": fid})
        if url == gdrive.UPLOAD:
            sid = "https://upload/session/%d" % len(self.sessions)
            self.sessions[sid] = {"meta": json, "data": b"", "size": int(headers["X-Upload-Content-Length"])}
            return Resp(200, {}, {"Location": sid})
        raise AssertionError(url)

    def put(self, url, data=None, headers=None, timeout=None):
        s = self.sessions[url]
        rng = headers["Content-Range"]
        if rng.startswith("bytes */"):
            pass  # status query
        else:
            if self.fail_next_put:
                self.fail_next_put = False
                # pretend half the chunk arrived, then the connection broke
                s["data"] += data[:len(data) // 2]
                raise self.RequestException("connection reset")
            start = int(rng.split()[1].split("-")[0])
            assert start == len(s["data"]), (start, len(s["data"]))
            s["data"] += data
        if len(s["data"]) >= s["size"]:
            fid = self._id()
            self.files[fid] = {"name": s["meta"]["name"], "parents": s["meta"]["parents"], "mime": "x", "data": s["data"]}
            return Resp(200, {"id": fid})
        return Resp(308, None, {"Range": "bytes=0-%d" % (len(s["data"]) - 1)} if s["data"] else {})

    def get(self, url, headers=None, params=None, timeout=None, stream=False):
        if url == gdrive.API + "/about":
            return Resp(200, {"user": {"emailAddress": "me@example.com"}})
        if url == gdrive.API + "/files":
            q = params["q"]
            if "in parents" in q:
                parent = q.split("'")[1]
                out = [dict(id=i, name=f["name"], size=str(len(f["data"])), createdTime="2026-11-03T00:00:00Z")
                       for i, f in self.files.items() if parent in f["parents"]]
            else:
                name = q.split("name='")[1].split("'")[0]
                out = [dict(id=i, name=f["name"]) for i, f in self.files.items()
                       if f["mime"] == gdrive.FOLDER_MIME and f["name"] == name]
            return Resp(200, {"files": out})
        fid = url.rsplit("/", 1)[1]
        return Resp(200, None, {"Content-Length": str(len(self.files[fid]["data"]))}, self.files[fid]["data"])

    def delete(self, url, headers=None, params=None, timeout=None):
        self.files.pop(url.rsplit("/", 1)[1], None)
        return Resp(204)


class DriveTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeDrive()
        p = mock.patch.object(gdrive, "requests", self.fake)
        p.start()
        self.addCleanup(p.stop)
        self.sleep = mock.patch.object(gdrive.time, "sleep", lambda s: None)
        self.sleep.start()
        self.addCleanup(self.sleep.stop)
        gdrive.forget_token("d1")
        self.dest = {"id": "d1", "type": "gdrive", "auth": "oauth", "client_id": "c", "client_secret": "s",
                     "refresh_token": "r", "folder_name": "MariaDB Backups"}
        self.tmp = tempfile.mkdtemp()
        mock.patch.object(gdrive, "CHUNK", 256 * 1024).start()
        self.addCleanup(mock.patch.stopall)

    def make(self, name, size):
        p = os.path.join(self.tmp, name)
        with open(p, "wb") as f:
            f.write(os.urandom(size))
        return p

    def test_round_trip_and_retention(self):
        d = destinations.GoogleDrive(self.dest)
        self.assertIn("me@example.com", d.test())
        folders = [f for f in self.fake.files.values() if f["mime"] == gdrive.FOLDER_MIME]
        self.assertEqual(len(folders), 1)
        d.test()  # the folder is found again, not created twice
        self.assertEqual(len([f for f in self.fake.files.values() if f["mime"] == gdrive.FOLDER_MIME]), 1)

        names = ["nightly-20261101-020000.mdbk.tar", "nightly-20261102-020000.mdbk.tar", "nightly-20261103-020000.mdbk.tar"]
        originals = {}
        for i, n in enumerate(names):
            p = self.make(n, 600 * 1024 + i)
            originals[n] = Path(p).read_bytes()
            if i == 1:
                self.fake.fail_next_put = True  # the second upload loses a chunk half way
            d.store(p)
            self.assertFalse(os.path.exists(p))
        listed = d.list()
        self.assertEqual([e["name"] for e in listed], names[::-1])

        got = d.fetch(names[1])
        self.assertEqual(Path(got).read_bytes(), originals[names[1]])

        removed = destinations.prune(d, "nightly", 2)
        self.assertEqual(removed, [names[0]])
        self.assertEqual([e["name"] for e in d.list()], names[:0:-1])

    def test_rejects_odd_names(self):
        d = destinations.GoogleDrive(self.dest)
        for bad in ("../etc/passwd", "x.tar", ".hidden-20261101-020000.mdbk.tar"):
            with self.assertRaises(Exception):
                d.fetch(bad)


if __name__ == "__main__":
    unittest.main()
