"""Backup and restore engine.

A backup is one consistent ``mariadb-dump --databases`` stream (structure,
data, indexes, views, triggers, routines, events) split as it is read into
one gzip file per object, plus a logical export of users, roles and grants,
plus a manifest with checksums. The pieces are packed into a single
uncompressed ``.tar`` so a backup is one file wherever it is stored.

Restore reads members straight out of the tar and replays only the pieces
that were picked, so a single table, a single user or a whole server can be
brought back from the same file.

Only the standard library and the mariadb / mariadb-dump client programs are
used, so this module also runs on its own (see mbm/cli.py).
"""

import datetime as _dt
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading

FORMAT_VERSION = 1
SYSTEM_DATABASES = {"information_schema", "performance_schema", "sys", "mysql"}
SYSTEM_ACCOUNTS = {("mariadb.sys", "localhost"), ("mysql", "localhost"),
                   ("root", "localhost"), ("root", "127.0.0.1"), ("root", "::1")}
ARCHIVE_SUFFIX = ".mdbk.tar"


class EngineError(Exception):
    pass


class Cancelled(EngineError):
    pass


def _noop(_msg):
    pass


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def qident(name):
    return "`" + name.replace("`", "``") + "`"


def qstr(value):
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def qaccount(user, host):
    return "%s@%s" % (qident(user), qident(host))


def account_label(user, host):
    return "%s@%s" % (user, host)


def human_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024


def slug(text):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text or "").strip("_") or "backup"


def unescape_batch(value):
    """Undo the escaping the mariadb client applies in --batch mode."""
    if value == "NULL":
        return None
    if "\\" not in value:
        return value
    out, i = [], 0
    while i < len(value):
        c = value[i]
        if c == "\\" and i + 1 < len(value):
            n = value[i + 1]
            out.append({"n": "\n", "t": "\t", "0": "\0", "\\": "\\"}.get(n, n))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


class HashingFile:
    """File wrapper that sha256-hashes and counts everything written."""

    def __init__(self, path):
        self.f = open(path, "wb")
        self.sha = hashlib.sha256()
        self.size = 0

    def write(self, data):
        self.sha.update(data)
        self.size += len(data)
        return self.f.write(data)

    def flush(self):
        self.f.flush()

    def close(self):
        self.f.close()


def sha256_stream(f):
    h = hashlib.sha256()
    for chunk in iter(lambda: f.read(1 << 20), b""):
        h.update(chunk)
    return h.hexdigest()


def find_binary(*names):
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    raise EngineError("Could not find %s. Install the MariaDB client tools (mariadb-client)."
                      % " or ".join(names))


def _opt_value(v):
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


# --------------------------------------------------------------------------
# connection
# --------------------------------------------------------------------------

class Connection:
    """Runs the mariadb client tools for one server.

    ``server`` is a dict with any of host, port, socket, user, password.
    Credentials go into a private temporary option file, never onto a command
    line where other users could read them from the process list.
    """

    def __init__(self, server):
        self.server = dict(server or {})
        self.client = find_binary("mariadb", "mysql")
        lines = ["[client]"]
        for key in ("host", "port", "socket", "user", "password"):
            val = self.server.get(key)
            if val not in (None, ""):
                lines.append("%s=%s" % (key, _opt_value(val)))
        if self.server.get("ssl"):
            lines.append("ssl")
        fd, self.cnf = tempfile.mkstemp(prefix="mbm-", suffix=".cnf")
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(lines) + "\n")
        os.chmod(self.cnf, 0o600)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        try:
            os.unlink(self.cnf)
        except OSError:
            pass

    def _base(self, binary):
        return [binary, "--defaults-extra-file=" + self.cnf]

    def query(self, sql, timeout=60):
        cmd = self._base(self.client) + ["--batch", "--skip-column-names",
                                         "--default-character-set=utf8mb4",
                                         "--connect-timeout=10", "-e", sql]
        try:
            p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise EngineError("The server did not answer within %d seconds" % timeout)
        if p.returncode != 0:
            raise EngineError(p.stderr.decode("utf-8", "replace").strip() or "query failed")
        rows = []
        for line in p.stdout.decode("utf-8", "replace").split("\n"):
            if line:
                rows.append([unescape_batch(v) for v in line.split("\t")])
        return rows

    def scalar(self, sql):
        rows = self.query(sql)
        return rows[0][0] if rows and rows[0] else None

    def client_process(self, force=False):
        cmd = self._base(self.client) + ["--default-character-set=utf8mb4", "--batch"]
        if force:
            cmd.append("--force")
        return subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE)

    def dump_process(self, databases, lock_all=False, extra=None):
        dumper = find_binary("mariadb-dump", "mysqldump")
        cmd = self._base(dumper) + [
            "--default-character-set=utf8mb4",
            "--routines", "--triggers", "--events",
            "--hex-blob", "--quick", "--add-drop-table",
            # Galera replicates LOCK TABLES poorly; the restore never needs
            # them because it runs in a single session anyway.
            "--skip-add-locks",
        ]
        cmd.append("--lock-all-tables" if lock_all else "--single-transaction")
        cmd += list(extra or [])
        cmd += ["--databases"] + list(databases)
        errf = tempfile.TemporaryFile()
        return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf), errf


# --------------------------------------------------------------------------
# inspecting a server
# --------------------------------------------------------------------------

def server_info(conn):
    """Version, Galera state and databases of a server."""
    version = conn.scalar("SELECT VERSION()")
    status = {r[0].lower(): r[1] for r in conn.query(
        "SHOW GLOBAL STATUS WHERE Variable_name IN ('wsrep_cluster_size', "
        "'wsrep_local_state_comment', 'wsrep_cluster_status', 'wsrep_ready')")}
    wsrep_on = (conn.scalar("SELECT @@GLOBAL.wsrep_on") or "0") in ("1", "ON")
    rows = conn.query(
        "SELECT s.SCHEMA_NAME, COUNT(t.TABLE_NAME), "
        "COALESCE(SUM(t.DATA_LENGTH + t.INDEX_LENGTH), 0), "
        "COALESCE(SUM(t.TABLE_TYPE = 'BASE TABLE' AND t.ENGINE <> 'InnoDB'), 0) "
        "FROM information_schema.SCHEMATA s "
        "LEFT JOIN information_schema.TABLES t ON t.TABLE_SCHEMA = s.SCHEMA_NAME "
        "GROUP BY s.SCHEMA_NAME ORDER BY s.SCHEMA_NAME")
    dbs = [{"name": r[0], "objects": int(r[1]), "size": int(float(r[2] or 0)),
            "non_innodb": int(float(r[3] or 0)), "system": r[0] in SYSTEM_DATABASES} for r in rows]
    galera = None
    if wsrep_on:
        galera = {"cluster_size": int(status.get("wsrep_cluster_size") or 0),
                  "state": status.get("wsrep_local_state_comment"),
                  "cluster_status": status.get("wsrep_cluster_status"),
                  "ready": status.get("wsrep_ready")}
    return {"version": version, "galera": galera, "databases": dbs}


def export_accounts(conn, log=_noop):
    rows = conn.query("SELECT User, Host, is_role FROM mysql.user ORDER BY is_role DESC, User, Host")
    roles, users = [], []
    for user, host, is_role in rows:
        if is_role == "Y":
            grants = [r[0] for r in conn.query("SHOW GRANTS FOR %s" % qident(user))]
            roles.append({"name": user, "grants": grants})
            continue
        acct = qaccount(user, host)
        try:
            create = conn.scalar("SHOW CREATE USER %s" % acct)
            grants = [r[0] for r in conn.query("SHOW GRANTS FOR %s" % acct)]
        except EngineError as e:
            log("warning: could not export %s: %s" % (account_label(user, host), e))
            continue
        users.append({"user": user, "host": host, "create": create, "grants": grants,
                      "system": (user, host) in SYSTEM_ACCOUNTS})
    return {"roles": roles, "users": users}


# --------------------------------------------------------------------------
# splitting a dump
# --------------------------------------------------------------------------

MARKERS = [
    (re.compile(rb"^-- Current Database: `(.*)`$"), "database"),
    (re.compile(rb"^-- Table structure for table `(.*)`$"), "table"),
    (re.compile(rb"^-- Dumping data for table `(.*)`$"), "data"),
    (re.compile(rb"^-- Temporary (?:table|view) structure for view `(.*)`$"), "view_stub"),
    (re.compile(rb"^-- Final view structure for view `(.*)`$"), "view"),
    (re.compile(rb"^-- Dumping events for database '(.*)'$"), "events"),
    (re.compile(rb"^-- Dumping routines for database '(.*)'$"), "routines"),
]
FOOTER_START = b"/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;"
TRIGGER_START = b"/*!50003 SET @saved_cs_client"
DUMP_END = re.compile(rb"^-- Dump completed")
OBJECT_OF = {"table": "table", "data": "table", "triggers": "table",
             "view_stub": "view", "view": "view", "events": "events", "routines": "routines"}


def _unq(raw, kind):
    s = raw.decode("utf-8", "replace")
    return s.replace("''", "'") if kind in ("events", "routines") else s.replace("``", "`")


def split_dump(stream, outdir, level=6, progress=None, cancel=None):
    """Split a ``mariadb-dump --databases`` stream into per-object gzip files.

    Returns (header_bytes, footer_bytes, databases).
    """
    header, footer, dbs, by_name = [], [], [], {}
    st = {"db": None, "w": None, "sec": None, "zone": "header", "bytes": 0}

    def close_section():
        if st["w"] is None:
            return
        gz, hf = st["w"]
        gz.close()
        hf.close()
        st["sec"]["sha256"] = hf.sha.hexdigest()
        st["sec"]["bytes"] = hf.size
        st["w"] = st["sec"] = None

    def open_section(kind, name):
        close_section()
        db = st["db"]
        if db is None:
            raise EngineError("Unexpected dump layout: a section before any database")
        ddir = os.path.join(outdir, "databases", db["dir"])
        os.makedirs(ddir, exist_ok=True)
        fname = "%05d-%s.sql.gz" % (len(db["sections"]) + 1, kind)
        hf = HashingFile(os.path.join(ddir, fname))
        gz = gzip.GzipFile(filename="", mode="wb", fileobj=hf, compresslevel=level, mtime=0)
        sec = {"kind": kind, "object": name, "file": "databases/%s/%s" % (db["dir"], fname)}
        db["sections"].append(sec)
        st["w"], st["sec"] = (gz, hf), sec
        if progress and kind in ("table", "view", "routines", "events"):
            progress(db["name"], kind, name)

    n = 0
    for line in stream:
        n += 1
        if cancel is not None and n % 2000 == 0 and cancel.is_set():
            raise Cancelled("Cancelled")
        st["bytes"] += len(line)
        stripped = line.rstrip(b"\r\n")
        zone = st["zone"]
        if zone == "footer":
            if not DUMP_END.match(stripped):
                footer.append(line)
            continue
        if stripped == FOOTER_START and zone != "header":
            close_section()
            st["zone"] = "footer"
            footer.append(line)
            continue
        matched = None
        if stripped.startswith(b"-- "):
            for rx, kind in MARKERS:
                m = rx.match(stripped)
                if m:
                    matched = (kind, _unq(m.group(1), kind))
                    break
        if matched:
            kind, name = matched
            if kind == "database":
                close_section()
                st["zone"] = "dbpreamble"
                if name not in by_name:
                    db = {"name": name, "dir": "%03d" % (len(dbs) + 1), "create_sql": None,
                          "sections": []}
                    dbs.append(db)
                    by_name[name] = db
                st["db"] = by_name[name]
            else:
                st["zone"] = "section"
                open_section(kind, name)
            continue
        if stripped == b"--":
            continue
        if zone == "header":
            if not stripped.startswith(b"-- "):
                header.append(line)
            continue
        if zone == "dbpreamble":
            if stripped.startswith(b"CREATE DATABASE"):
                st["db"]["create_sql"] = stripped.decode("utf-8", "replace")
            continue
        if st["sec"]["kind"] == "data" and stripped.startswith(TRIGGER_START):
            open_section("triggers", st["sec"]["object"])
        st["w"][0].write(line)
    close_section()
    return b"".join(header), b"".join(footer), dbs, st["bytes"]


# --------------------------------------------------------------------------
# backup
# --------------------------------------------------------------------------

def run_backup(server, databases, include_users=True, workdir=None, label="backup",
               lock_all=False, desync=False, compress_level=6, log=_noop, cancel=None,
               source_name=None):
    """Take a backup and return the path of the finished .tar in ``workdir``.

    ``databases`` is a list of names or the string "all" (every non-system
    database). With ``desync`` on a Galera node, the node is taken out of flow
    control for the duration so a long dump does not slow the cluster down.
    """
    workdir = workdir or tempfile.gettempdir()
    os.makedirs(workdir, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    name = "%s-%s" % (slug(label), stamp)
    stage = tempfile.mkdtemp(prefix=name + ".", dir=workdir)
    os.chmod(stage, 0o700)
    tar_path = os.path.join(workdir, name + ARCHIVE_SUFFIX)
    desynced = False
    with Connection(server) as conn:
        try:
            info = server_info(conn)
            names = [d["name"] for d in info["databases"]]
            if databases == "all" or databases is None:
                selected = [d["name"] for d in info["databases"] if not d["system"]]
            else:
                missing = [d for d in databases if d not in names]
                if missing:
                    raise EngineError("Database(s) not on the server: %s" % ", ".join(missing))
                selected = list(databases)
            if not selected and not include_users:
                raise EngineError("Nothing to back up: no databases selected and users excluded.")
            log("Server %s%s" % (info["version"], ", Galera node (%s, cluster of %d)" % (
                info["galera"]["state"], info["galera"]["cluster_size"]) if info["galera"] else ""))
            odd = [d for d in info["databases"] if d["name"] in selected and d["non_innodb"]]
            for d in odd:
                log("warning: %s has %d non-InnoDB table(s); they are not covered by the "
                    "transaction snapshot%s" % (d["name"], d["non_innodb"],
                                                " and Galera does not replicate them" if info["galera"] else ""))
            if info["galera"] and desync:
                conn.query("SET GLOBAL wsrep_desync = ON")
                desynced = True
                log("Galera: node desynced for the backup (wsrep_desync=ON)")

            manifest = {
                "format": FORMAT_VERSION, "label": label, "name": name,
                "created": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                "server_version": info["version"],
                "source": source_name or server.get("host") or "localhost",
                "galera": bool(info["galera"]),
                "consistency": "lock-all-tables" if lock_all else "single-transaction",
                "databases": [], "accounts": None, "dump_bytes": 0,
            }
            if selected:
                log("Dumping %d database(s): %s" % (len(selected), ", ".join(selected)))
                proc, errf = conn.dump_process(selected, lock_all)
                seen = set()

                def progress(db, kind, obj):
                    if db not in seen:
                        seen.add(db)
                        log("  dumping %s" % db)

                try:
                    header, footer, dbs, nbytes = split_dump(proc.stdout, stage, compress_level,
                                                             progress, cancel)
                except BaseException:
                    proc.kill()
                    proc.wait()
                    raise
                proc.stdout.close()
                rc = proc.wait()
                errf.seek(0)
                err = errf.read().decode("utf-8", "replace").strip()
                if rc != 0:
                    raise EngineError("mariadb-dump failed (exit %d): %s" % (rc, err))
                if err:
                    log("mariadb-dump: " + err)
                with open(os.path.join(stage, "header.sql"), "wb") as f:
                    f.write(header)
                with open(os.path.join(stage, "footer.sql"), "wb") as f:
                    f.write(footer)
                for db in dbs:
                    s = summarize_db(db)
                    log("  %-28s %d tables, %d views%s%s" % (
                        db["name"], s["tables"], s["views"],
                        ", routines" if s["routines"] else "", ", events" if s["events"] else ""))
                manifest["databases"] = dbs
                manifest["dump_bytes"] = nbytes
            if include_users:
                accts = export_accounts(conn, log)
                data = json.dumps(accts, indent=2).encode()
                with open(os.path.join(stage, "accounts.json"), "wb") as f:
                    f.write(data)
                manifest["accounts"] = {"file": "accounts.json",
                                        "sha256": hashlib.sha256(data).hexdigest(),
                                        "users": len(accts["users"]), "roles": len(accts["roles"])}
                log("Accounts: %d users, %d roles" % (len(accts["users"]), len(accts["roles"])))
        finally:
            if desynced:
                try:
                    conn.query("SET GLOBAL wsrep_desync = OFF")
                    log("Galera: node back in sync (wsrep_desync=OFF)")
                except EngineError as e:
                    log("warning: could not turn wsrep_desync off again: %s" % e)

    try:
        with open(os.path.join(stage, "manifest.json"), "w") as f:
            json.dump(manifest, f, indent=2)
        partial = tar_path + ".partial"
        with tarfile.open(partial, "w", format=tarfile.PAX_FORMAT) as tar:
            # manifest first, so listing a backup reads only the start of the file
            for rel in ["manifest.json", "header.sql", "footer.sql", "accounts.json"]:
                p = os.path.join(stage, rel)
                if os.path.exists(p):
                    tar.add(p, arcname=rel)
            for db in manifest["databases"]:
                for s in db["sections"]:
                    tar.add(os.path.join(stage, s["file"]), arcname=s["file"])
        os.chmod(partial, 0o600)
        os.rename(partial, tar_path)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        if os.path.exists(tar_path + ".partial"):
            os.unlink(tar_path + ".partial")
    log("Backup file %s (%s)" % (os.path.basename(tar_path), human_size(os.path.getsize(tar_path))))
    return tar_path, manifest


def summarize_db(db):
    tables, views, kinds = [], [], set()
    for s in db["sections"]:
        kinds.add(s["kind"])
        if s["kind"] == "table" and s["object"] not in tables:
            tables.append(s["object"])
        if s["kind"] in ("view", "view_stub") and s["object"] not in views:
            views.append(s["object"])
    return {"tables": len(tables), "views": len(views), "table_names": tables, "view_names": views,
            "routines": _nonempty(db, "routines"), "events": _nonempty(db, "events"),
            "bytes": sum(s.get("bytes", 0) for s in db["sections"])}


def _nonempty(db, kind):
    # mariadb-dump writes a nearly empty routines/events section even when
    # there are none; anything under ~60 compressed bytes holds no objects.
    return any(s["kind"] == kind and s.get("bytes", 0) > 60 for s in db["sections"])


# --------------------------------------------------------------------------
# reading a backup
# --------------------------------------------------------------------------

class Archive:
    """Read access to a backup .tar (or an unpacked backup directory)."""

    def __init__(self, path):
        self.path = path
        self.tar = None
        if os.path.isdir(path):
            self._open = lambda rel: open(os.path.join(path, rel), "rb")
            self._exists = lambda rel: os.path.isfile(os.path.join(path, rel))
        else:
            try:
                self.tar = tarfile.open(path, "r:")
            except (tarfile.TarError, OSError) as e:
                raise EngineError("%s is not a readable backup: %s" % (os.path.basename(path), e))
            self._members = {m.name: m for m in self.tar.getmembers()}
            self._open = lambda rel: self.tar.extractfile(self._members[rel])
            self._exists = lambda rel: rel in self._members
        if not self._exists("manifest.json"):
            raise EngineError("%s has no manifest.json; it is not a backup made by this tool"
                              % os.path.basename(path))
        with self._open("manifest.json") as f:
            self.manifest = json.load(f)
        if self.manifest.get("format") != FORMAT_VERSION:
            raise EngineError("Unsupported backup format %r" % self.manifest.get("format"))
        self.accounts = None
        acc = self.manifest.get("accounts")
        if acc and self._exists(acc["file"]):
            with self._open(acc["file"]) as f:
                self.accounts = json.load(f)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self.tar:
            self.tar.close()

    def read(self, rel):
        if not self._exists(rel):
            return b""
        with self._open(rel) as f:
            return f.read()

    def open_section(self, sec):
        return gzip.GzipFile(fileobj=self._open(sec["file"]), mode="rb")

    def contents(self):
        """A description of everything restorable, for the UI and `list`."""
        m = self.manifest
        dbs = []
        for db in m["databases"]:
            s = summarize_db(db)
            dbs.append({"name": db["name"], "tables": s["table_names"], "views": s["view_names"],
                        "routines": s["routines"], "events": s["events"], "bytes": s["bytes"]})
        accts = {"roles": [], "users": []}
        if self.accounts:
            accts["roles"] = [r["name"] for r in self.accounts["roles"]]
            accts["users"] = [{"name": account_label(u["user"], u["host"]), "system": u["system"]}
                              for u in self.accounts["users"]]
        return {"name": m.get("name"), "label": m.get("label"), "created": m["created"],
                "source": m.get("source"), "server_version": m["server_version"],
                "galera": m.get("galera"), "consistency": m.get("consistency"),
                "databases": dbs, "accounts": accts, "has_accounts": self.accounts is not None}

    def verify(self):
        problems = []
        files = [(s["file"], s["sha256"]) for db in self.manifest["databases"] for s in db["sections"]]
        if self.manifest.get("accounts"):
            files.append((self.manifest["accounts"]["file"], self.manifest["accounts"]["sha256"]))
        for rel, want in files:
            if not self._exists(rel):
                problems.append("missing " + rel)
                continue
            with self._open(rel) as f:
                if sha256_stream(f) != want:
                    problems.append("checksum mismatch " + rel)
                    continue
            if rel.endswith(".gz"):
                try:
                    with gzip.GzipFile(fileobj=self._open(rel)) as g:
                        while g.read(1 << 20):
                            pass
                except (OSError, EOFError):
                    problems.append("bad gzip " + rel)
        return len(files), problems


def db_objects(db):
    s = summarize_db(db)
    objs = [("table", t) for t in s["table_names"]] + [("view", v) for v in s["view_names"]]
    if s["routines"]:
        objs.append(("routines", None))
    if s["events"]:
        objs.append(("events", None))
    return objs


# --------------------------------------------------------------------------
# restore
# --------------------------------------------------------------------------

def normalize_plan(archive, plan):
    """Validate a restore plan and resolve it against the backup.

    plan = {
      "databases": [{"name": "shop", "target": "shop", "objects": None | [[kind, name], ...]}],
      "roles": ["readonly"], "users": ["app@%"],
      "schema_only": False, "replace_users": False, "force": False,
    }
    ``objects`` None means everything in that database; kinds are table, view,
    routines and events (name null for the last two).
    """
    by_name = {d["name"]: d for d in archive.manifest["databases"]}
    out_dbs = []
    for spec in plan.get("databases") or []:
        db = by_name.get(spec.get("name"))
        if not db:
            raise EngineError("Database %r is not in this backup" % spec.get("name"))
        target = (spec.get("target") or db["name"]).strip()
        if not target or len(target) > 64:
            raise EngineError("Bad target database name %r" % target)
        objs = spec.get("objects")
        if objs is not None:
            valid = set(db_objects(db))
            objs = set((o[0], o[1] if o[0] in ("table", "view") else None) for o in objs)
            bad = objs - valid
            if bad:
                raise EngineError("Not in %s: %s" % (db["name"], ", ".join("%s %s" % b for b in bad)))
            if not objs:
                continue
        out_dbs.append((db, target, objs))
    targets = [t for _, t, _ in out_dbs]
    if len(set(targets)) != len(targets):
        raise EngineError("Two databases would be restored under the same name")
    roles, users = [], []
    acc = archive.accounts or {"roles": [], "users": []}
    want_roles = set(plan.get("roles") or [])
    want_users = set(plan.get("users") or [])
    if (want_roles or want_users) and not archive.accounts:
        raise EngineError("This backup has no users or grants")
    roles = [r for r in acc["roles"] if r["name"] in want_roles]
    users = [u for u in acc["users"] if account_label(u["user"], u["host"]) in want_users]
    missing = (want_roles - {r["name"] for r in roles}) | \
              (want_users - {account_label(u["user"], u["host"]) for u in users})
    if missing:
        raise EngineError("Not in this backup: %s" % ", ".join(sorted(missing)))
    if not out_dbs and not roles and not users:
        raise EngineError("Nothing selected to restore")
    return {"dbs": out_dbs, "roles": roles, "users": users,
            "schema_only": bool(plan.get("schema_only")),
            "replace_users": bool(plan.get("replace_users")),
            "force": bool(plan.get("force"))}


def selected_sections(db, objects, schema_only):
    out = []
    for s in db["sections"]:
        kind = OBJECT_OF[s["kind"]]
        obj = (kind, None) if kind in ("routines", "events") else (kind, s["object"])
        if objects is not None and obj not in objects:
            continue
        if schema_only and s["kind"] == "data":
            continue
        out.append(s)
    return out


def describe(norm):
    lines = []
    for db, target, objs in norm["dbs"]:
        if objs is None:
            what = "everything"
        else:
            what = ", ".join(("%s %s" % o) if o[1] else o[0] for o in sorted(objs, key=str))
        arrow = "" if target == db["name"] else " as %s" % target
        lines.append("database %s%s: %s%s" % (db["name"], arrow, what,
                                              " (structure only)" if norm["schema_only"] else ""))
    for r in norm["roles"]:
        lines.append("role %s" % r["name"])
    for u in norm["users"]:
        lines.append("user %s" % account_label(u["user"], u["host"]))
    return lines


def preflight(conn, norm):
    """What a restore would overwrite on the target server."""
    warnings = []
    existing = {r[0] for r in conn.query("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA")}
    for db, target, objs in norm["dbs"]:
        if target not in existing:
            continue
        present = {r[0] for r in conn.query(
            "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s" % qstr(target))}
        mine = {o[1] for o in (db_objects(db) if objs is None else objs) if o[1]}
        clash = sorted(present & mine)
        if clash:
            warnings.append("%s: %d existing table/view(s) will be dropped and replaced (%s%s)" % (
                target, len(clash), ", ".join(clash[:8]), " ..." if len(clash) > 8 else ""))
    if norm["users"]:
        have = {(r[0], r[1]) for r in conn.query("SELECT User, Host FROM mysql.user")}
        clash = [account_label(u["user"], u["host"]) for u in norm["users"] if (u["user"], u["host"]) in have]
        if clash:
            warnings.append("%d user(s) already exist and will be %s: %s" % (
                len(clash), "replaced" if norm["replace_users"] else "kept (grants added)",
                ", ".join(clash[:8])))
    return warnings


def rewrite_create_db(create_sql, source, target):
    if not create_sql:
        return "CREATE DATABASE IF NOT EXISTS %s;" % qident(target)
    return create_sql.replace(qident(source), qident(target), 1)


class LineTracker:
    """Writes to a sink, remembering which object each output line belongs to."""

    def __init__(self, sink, cancel=None):
        self.sink, self.cancel = sink, cancel
        self.line, self.marks, self.bytes = 1, [], 0

    def mark(self, label):
        self.marks.append((self.line, label))

    def write(self, data):
        if self.cancel is not None and self.cancel.is_set():
            raise Cancelled("Cancelled")
        self.line += data.count(b"\n")
        self.bytes += len(data)
        self.sink.write(data)

    def label_for(self, lineno):
        found = None
        for start, lab in self.marks:
            if start <= lineno:
                found = lab
        return found


_IDENT_CLAUSE = re.compile(r" IDENTIFIED (?:BY PASSWORD '[^']*'|VIA .*?)(?= WITH |$)")


def account_statements(norm):
    replace = norm["replace_users"]
    stmts = []
    for r in norm["roles"]:
        verb = "CREATE OR REPLACE ROLE" if replace else "CREATE ROLE IF NOT EXISTS"
        stmts.append(("%s %s" % (verb, qident(r["name"])), "role " + r["name"]))
    for u in norm["users"]:
        verb = "CREATE OR REPLACE USER" if replace else "CREATE USER IF NOT EXISTS"
        create = re.sub(r"^CREATE USER", verb, u["create"].replace("\n", " "), count=1)
        stmts.append((create, "user " + account_label(u["user"], u["host"])))
    for r in norm["roles"]:
        for g in r["grants"]:
            stmts.append((g.replace("\n", " "), "role " + r["name"]))
    for u in norm["users"]:
        for g in u["grants"]:
            # never let a GRANT reset the password of a user that was kept
            stmts.append((_IDENT_CLAUSE.sub("", g.replace("\n", " ")),
                          "user " + account_label(u["user"], u["host"])))
    return stmts


def write_db_sql(out, archive, header, footer, db, target, objects, schema_only, log=_noop):
    out.mark("database %s" % target)
    out.write(header)
    out.write(("\n%s\nUSE %s;\n" % (rewrite_create_db(db["create_sql"], db["name"], target),
                                     qident(target))).encode())
    last = None
    for s in selected_sections(db, objects, schema_only):
        name = s["object"] if s["kind"] not in ("routines", "events") else None
        label = "%s.%s" % (target, name) if name else "%s %s" % (target, s["kind"])
        if label != last:
            log("  " + label)
            last = label
        out.mark("%s (%s)" % (label, s["kind"]))
        with archive.open_section(s) as g:
            for chunk in iter(lambda: g.read(1 << 20), b""):
                out.write(chunk)
        out.write(b"\n")
    out.mark("database %s" % target)
    out.write(footer)


def write_restore_sql(out, archive, norm, log=_noop):
    header, footer = archive.read("header.sql"), archive.read("footer.sql")
    for db, target, objs in norm["dbs"]:
        write_db_sql(out, archive, header, footer, db, target, objs, norm["schema_only"], log)
    stmts = account_statements(norm)
    for sql, lab in stmts:
        out.mark(lab)
        out.write((sql + ";\n").encode())
    if stmts:
        out.write(b"FLUSH PRIVILEGES;\n")


def _feed(conn, producer, force, cancel=None):
    proc = conn.client_process(force=force)
    errbuf = []

    def drain():
        for line in iter(proc.stderr.readline, b""):
            errbuf.append(line.decode("utf-8", "replace").rstrip())

    t = threading.Thread(target=drain, daemon=True)
    t.start()
    tracker = LineTracker(proc.stdin, cancel)
    broken = False
    try:
        producer(tracker)
    except BrokenPipeError:
        broken = True
    except Cancelled:
        proc.kill()
        proc.wait()
        raise
    try:
        proc.stdin.close()
    except BrokenPipeError:
        broken = True
    rc = proc.wait()
    t.join()
    errors = []
    for line in errbuf:
        m = re.match(r"^ERROR \d+ \(\w+\) at line (\d+)", line)
        where = tracker.label_for(int(m.group(1))) if m else None
        errors.append("%s%s" % ("[%s] " % where if where else "", line))
    if rc != 0 and not errors:
        errors.append("mariadb client exited with code %d%s" % (rc, " (input cut off)" if broken else ""))
    return errors


def run_restore(server, archive, plan, log=_noop, cancel=None):
    """Restore the picked pieces of ``archive`` onto ``server``. Returns error lines."""
    norm = normalize_plan(archive, plan)
    for line in describe(norm):
        log("Will restore " + line)
    header, footer = archive.read("header.sql"), archive.read("footer.sql")
    errors = []
    with Connection(server) as conn:
        info = server_info(conn)
        if info["galera"]:
            log("Target is a Galera node (%s); changes replicate to the rest of the cluster"
                % info["galera"]["state"])
        for w in preflight(conn, norm):
            log("note: " + w)
        if norm["dbs"]:
            log("Restoring databases")
            errors += _feed(conn, lambda out: [write_db_sql(
                out, archive, header, footer, db, target, objs, norm["schema_only"], log)
                for db, target, objs in norm["dbs"]], norm["force"], cancel)
            if errors and not norm["force"]:
                log("Stopped at the first error (turn on 'continue past errors' to carry on)")
        if (norm["roles"] or norm["users"]) and (not errors or norm["force"]):
            log("Restoring %d role(s) and %d user(s)" % (len(norm["roles"]), len(norm["users"])))
            stmts = account_statements(norm)

            def produce(out):
                for sql, lab in stmts:
                    out.mark(lab)
                    out.write((sql + ";\n").encode())
                out.write(b"FLUSH PRIVILEGES;\n")
            # one bad grant (say, on a table that was not restored) must not stop the rest
            errors += _feed(conn, produce, True, cancel)
    for e in errors:
        log("error: " + e)
    return errors
