"""Command-line access to the engine, for scripts and for disaster recovery.

The web UI is the normal way in. This exists for when the machine that ran it
is gone: copy a backup file anywhere with the mariadb client installed and

    python3 -m mbm.cli list   shop-20261103-020000.mdbk.tar
    python3 -m mbm.cli restore shop-20261103-020000.mdbk.tar --all -H db1 -u root -p

It needs nothing beyond the Python standard library.
"""

import argparse
import getpass
import os
import sys

from mbm import engine


def _server(args):
    pw = None
    if getattr(args, "password", False):
        pw = getpass.getpass("MariaDB password: ")
    elif os.environ.get("MBM_DB_PASSWORD") is not None:
        pw = os.environ["MBM_DB_PASSWORD"]
    return {"host": args.host, "port": args.port, "socket": args.socket,
            "user": args.user, "password": pw}


def _log(msg):
    print(msg, file=sys.stderr)


def cmd_backup(args):
    dbs = "all" if args.all or not args.database else args.database
    path, _ = engine.run_backup(_server(args), dbs, include_users=not args.no_users,
                                workdir=args.output, label=args.label or "backup",
                                lock_all=args.lock_all, desync=args.desync, log=_log)
    print(path)
    return 0


def cmd_list(args):
    with engine.Archive(args.backup) as a:
        c = a.contents()
    print("%s  taken %s from %s (MariaDB %s%s)" % (c["name"], c["created"], c["source"],
                                                   c["server_version"], ", Galera" if c["galera"] else ""))
    for db in c["databases"]:
        print("\ndatabase %s (%s compressed)" % (db["name"], engine.human_size(db["bytes"])))
        if db["tables"]:
            print("  tables: " + ", ".join(db["tables"]))
        if db["views"]:
            print("  views:  " + ", ".join(db["views"]))
        extra = [k for k in ("routines", "events") if db[k]]
        if extra:
            print("  also:   " + ", ".join(extra))
    if c["has_accounts"]:
        print("\nroles: " + (", ".join(c["accounts"]["roles"]) or "none"))
        print("users: " + ", ".join(u["name"] + (" [system]" if u["system"] else "")
                                    for u in c["accounts"]["users"]))
    return 0


def cmd_verify(args):
    with engine.Archive(args.backup) as a:
        n, problems = a.verify()
    for p in problems:
        print(p)
    print("%d files checked, %d problem(s)" % (n, len(problems)))
    return 1 if problems else 0


def build_plan(args, archive):
    c = archive.contents()
    renames = dict(r.split("=", 1) for r in args.rename or [])
    picked = {}
    if args.all:
        for db in c["databases"]:
            picked[db["name"]] = None
    for spec in args.db or []:
        name, _, obj = spec.partition(".")
        if not obj:
            picked[name] = None
        elif picked.get(name, []) is not None:
            kind = obj if obj in ("routines", "events") else None
            if kind is None:
                db = next((d for d in c["databases"] if d["name"] == name), None)
                if db is None:
                    raise engine.EngineError("Database %r is not in this backup" % name)
                kind = "view" if obj in db["views"] else "table"
            picked.setdefault(name, []).append([kind, None if kind in ("routines", "events") else obj])
    users, roles = [], []
    specs = list(args.users or [])
    if args.all and not args.no_users and not specs:
        specs = ["all"]
    for s in specs:
        if s == "all":
            roles = list(c["accounts"]["roles"])
            users = [u["name"] for u in c["accounts"]["users"]
                     if not u["system"] or args.include_system_users]
        elif s in c["accounts"]["roles"]:
            roles.append(s)
        else:
            users.append(s)
    return {"databases": [{"name": n, "target": renames.get(n, n), "objects": o} for n, o in picked.items()],
            "roles": roles, "users": users, "schema_only": args.schema_only,
            "replace_users": args.replace_users, "force": args.force}


def cmd_restore(args):
    with engine.Archive(args.backup) as a:
        plan = build_plan(args, a)
        norm = engine.normalize_plan(a, plan)
        if args.sql_out:
            with open(args.sql_out, "wb") as f:
                engine.write_restore_sql(engine.LineTracker(f), a, norm)
            _log("Wrote %s; nothing was changed on any server." % args.sql_out)
            return 0
        server = _server(args)
        with engine.Connection(server) as conn:
            for w in engine.preflight(conn, norm):
                _log("note: " + w)
        _log("About to restore:\n  " + "\n  ".join(engine.describe(norm)))
        if not args.yes:
            if not sys.stdin.isatty() or input("Go ahead? [y/N] ").strip().lower() not in ("y", "yes"):
                _log("Cancelled.")
                return 1
        errors = engine.run_restore(server, a, plan, log=_log)
    _log("Restore complete." if not errors else "Finished with %d error(s)." % len(errors))
    return 2 if errors else 0


def main(argv=None):
    conn = argparse.ArgumentParser(add_help=False)
    g = conn.add_argument_group("connection")
    g.add_argument("-H", "--host")
    g.add_argument("-P", "--port", type=int)
    g.add_argument("-S", "--socket")
    g.add_argument("-u", "--user")
    g.add_argument("-p", "--password", action="store_true",
                   help="prompt for the password (or set MBM_DB_PASSWORD)")

    p = argparse.ArgumentParser(prog="mbm", description="MariaDB Backup Manager command line")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backup", parents=[conn])
    b.add_argument("-a", "--all", action="store_true")
    b.add_argument("-d", "--database", action="append")
    b.add_argument("--no-users", action="store_true")
    b.add_argument("-o", "--output", default=".")
    b.add_argument("--label")
    b.add_argument("--lock-all", action="store_true")
    b.add_argument("--desync", action="store_true", help="Galera: set wsrep_desync during the dump")
    b.set_defaults(func=cmd_backup)

    r = sub.add_parser("restore", parents=[conn])
    r.add_argument("backup")
    r.add_argument("-a", "--all", action="store_true", help="every database and non-system account")
    r.add_argument("--db", action="append", metavar="DB[.TABLE|.VIEW|.routines|.events]")
    r.add_argument("--users", action="append", metavar="USER@HOST|ROLE|all")
    r.add_argument("--no-users", action="store_true")
    r.add_argument("--include-system-users", action="store_true")
    r.add_argument("--rename", action="append", metavar="OLD=NEW")
    r.add_argument("--schema-only", action="store_true")
    r.add_argument("--replace-users", action="store_true")
    r.add_argument("--force", action="store_true")
    r.add_argument("-y", "--yes", action="store_true")
    r.add_argument("--sql-out", metavar="FILE")
    r.set_defaults(func=cmd_restore)

    for name, fn in (("list", cmd_list), ("verify", cmd_verify)):
        s = sub.add_parser(name)
        s.add_argument("backup")
        s.set_defaults(func=fn)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except engine.EngineError as e:
        _log("error: %s" % e)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
