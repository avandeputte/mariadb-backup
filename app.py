#!/usr/bin/env python3
"""MariaDB Backup Manager -- a small self-hosted web UI that backs up MariaDB
(and Galera) servers on demand or on a schedule, to a local folder or Google
Drive, keeps the newest N backups, and restores exactly the databases, tables
and users you pick.

    app.py                                   run the web UI (waitress)
    app.py set-admin <username> <password|-> create/replace the UI login
    app.py show-admin                        print the configured username
    app.py cli ...                           the command-line tool (see mbm/cli.py)

Requires: Flask, waitress, requests, and the mariadb client programs.
"""

import sys

from mbm.base import DATA_DIR, LISTEN, PORT, THREADS, VERSION, app, load_config, log, setup_logging, update_config


def _cli(argv):
    cmd = argv[0]
    if cmd == "set-admin":
        from mbm.auth import set_admin
        if len(argv) != 3:
            print("usage: app.py set-admin <username> <password|->", file=sys.stderr)
            return 2
        password = sys.stdin.read().rstrip("\n") if argv[2] == "-" else argv[2]
        if len(password) < 8:
            print("the password must be at least 8 characters", file=sys.stderr)
            return 1
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        update_config(lambda cfg: set_admin(cfg, argv[1], password))
        print("administrator '%s' set" % argv[1])
        return 0
    if cmd == "show-admin":
        print((load_config().get("admin") or {}).get("username") or "(none)")
        return 0
    if cmd == "cli":
        from mbm.cli import main
        return main(argv[1:])
    if cmd in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if cmd == "--version":
        print(VERSION)
        return 0
    print("unknown command %r (try --help)" % cmd, file=sys.stderr)
    return 2


def main():
    if len(sys.argv) > 1:
        return _cli(sys.argv[1:])
    setup_logging()
    from mbm import api, auth, tasks  # noqa: F401  (registers routes)
    auth.seed_admin_from_env()
    load_config()
    tasks.start_background()
    log.info("MariaDB Backup Manager %s listening on http://%s:%d", VERSION, LISTEN, PORT)
    from waitress import serve
    # One process, a thread pool: the run queue and scheduler live in this
    # process, so it must not be forked into several workers.
    serve(app, host=LISTEN, port=PORT, threads=THREADS, ident="mbm", channel_timeout=3600)
    return 0


if __name__ == "__main__":
    sys.exit(main())
