# MariaDB Backup Manager

A small self-hosted web UI that backs up **MariaDB** and **Galera** servers on
demand or on a schedule, to a **local folder** or **Google Drive**, keeps the
newest *N* backups of each job, and **restores exactly what you pick**: whole
databases, single tables or views, stored routines, events, users and roles,
onto the same server or another, under the same name or a new one.

A backup holds everything needed to rebuild the instance from scratch:
databases with their character sets, tables (structure and data), indexes,
foreign keys, views, triggers, stored procedures and functions, events, and
every user and role with its grants and password hash.

```bash
# from a package: .deb and .rpm on every release (Debian, Ubuntu, RHEL, Fedora)
sudo apt-get install -y ./mariadb-backup-manager_0.1.0_all.deb

# or the install script, on any Debian-based server
curl -fsSL https://raw.githubusercontent.com/avandeputte/mariadb-backup/main/install.sh | sudo bash

# or in Docker (linux/amd64 and linux/arm64)
docker run -d -p 8090:8090 -v mbm-data:/data -v /srv/mariadb-backups:/backups \
  -e MBM_ADMIN_PASSWORD=change-me-please ghcr.io/avandeputte/mariadb-backup:latest

# or as a Proxmox VE LXC, from the Proxmox host (community-scripts engine)
bash -c "$(curl -fsSL https://raw.githubusercontent.com/avandeputte/mariadb-backup/main/proxmox/ct/mariadb-backup-manager.sh)"
```

Then open `http://<host>:8090`. The package, the install script and the
Proxmox script print a generated login (also kept in
`/var/lib/mariadb-backup-manager/admin-credentials.txt`); in Docker it is
`admin` with `MBM_ADMIN_PASSWORD`, or the UI asks you to create one on the first
visit.

## Using it

1. **Servers**: add the MariaDB server (host and port, or a local socket) and
   a user. *Test* shows the version, whether it is a Galera node and its state,
   and its databases.
2. **Destinations**: a local folder, or Google Drive (see below).
3. **Backup jobs**: pick the server, *all databases* (new ones are picked up
   automatically) or a list, whether to include users and grants, the
   destination, the schedule (hourly, daily, weekly, monthly or a cron
   expression; or on demand only), and **how many backups to keep**. After each
   successful backup, older backups of that job beyond that number are deleted
   from the destination. `0` keeps everything.
4. **Run now** on any job, or wait for the schedule. Every run has a live log
   under **Activity**.
5. **Backups & restore**: list a destination's backups, download, verify their
   checksums, delete, or **Restore...**: tick databases (everything, or pick
   tables, views, routines, events), optionally restore one under another name,
   tick users and roles, choose the target server and options (structure only,
   replace existing users, continue past errors), review what would be
   overwritten, and confirm.

## What a backup is

One file, `<job>-<YYYYmmdd-HHMMSS>.mdbk.tar`, containing:

* one consistent `mariadb-dump --single-transaction` of all the job's databases
  (one snapshot across databases), split into a gzip file per object so any
  piece can be restored on its own;
* `accounts.json`: every user (`SHOW CREATE USER`) and role with its grants;
* `manifest.json`: what is in it, the source server and version, and a SHA-256
  of every piece.

It is plain SQL inside a plain tar, so it is readable without this tool, and
the command line in this repository restores it on any machine with Python 3
and the MariaDB client, with no web UI and no settings:

```bash
python3 -m mbm.cli list    nightly-20261103-020000.mdbk.tar
python3 -m mbm.cli verify  nightly-20261103-020000.mdbk.tar
python3 -m mbm.cli restore nightly-20261103-020000.mdbk.tar --all -H db1 -u root -p
python3 -m mbm.cli restore nightly-20261103-020000.mdbk.tar --db shop.orders --rename shop=shop_old -H db1 -u root -p
python3 -m mbm.cli restore nightly-20261103-020000.mdbk.tar --db shop --sql-out shop.sql   # just write the SQL
```

The `mysql` system database itself is not dumped; users, roles and grants are
exported logically instead, which is what moves cleanly between versions and
servers. Server settings (`my.cnf`), plugins and binary logs are not part of a
backup.

## Galera

* Back up from one node. With **desync this node** on (job, advanced options),
  the node sets `wsrep_desync=ON` for the duration so a long dump does not
  hold the cluster back with flow control, and turns it off again afterwards,
  also when the backup fails. The user then needs `SUPER`.
* Only InnoDB is covered by the snapshot and replicated by Galera. Tables in
  other engines are reported in the run log and on the server test.
* Dumps are taken with `--skip-add-locks`, so restoring them never issues
  `LOCK TABLES`.
* Restore onto any one node: DDL, data, users and grants replicate to the rest
  of the cluster.

## Google Drive

**Your Google account (device sign-in).** Once, in the
[Google Cloud console](https://console.cloud.google.com/): create (or pick) a
project, enable the **Google Drive API**, set up the OAuth consent screen
(External, add yourself as a test user, or publish it), and create an
**OAuth client ID** of type **TVs and Limited Input devices**. In the UI, add a
Google Drive destination with that client ID and secret and a folder name, then
**Sign in with Google**: open the address shown on any device and type the code.
The app asks only for `drive.file`, so it sees the files and folder it creates
and nothing else in your Drive.

> An OAuth consent screen left in *Testing* issues sign-ins that Google expires
> after 7 days. Publish the app (no verification is needed for `drive.file`) to
> keep the sign-in working.

**Service account.** For a Google Workspace Shared Drive: create a service
account and a JSON key, add the service account to the Shared Drive as a
Content manager, and give the folder ID. Service accounts have no storage of
their own, so a folder in a personal My Drive does not work.

Uploads are resumable and retried; restores from Drive download the backup
into a cache first.

## Privileges

For backups:

```sql
CREATE USER 'backup'@'%' IDENTIFIED BY '...';
GRANT SELECT, SHOW VIEW, TRIGGER, EVENT, LOCK TABLES, SHOW DATABASES ON *.* TO 'backup'@'%';
-- Galera desync also needs: GRANT SUPER ON *.* TO 'backup'@'%';
```

Restores create databases, users and grants, so add a server entry with an
administrative account (for example root) for the server you restore onto.

## Files and settings

| | package / install.sh | Docker |
|---|---|---|
| app | `/opt/mariadb-backup-manager` | same |
| settings, schedules, history | `/var/lib/mariadb-backup-manager` | `/data` |
| default local backups | `/var/backups/mariadb` | `/backups` |
| port | 8090 | 8090 |

`config.json` holds server passwords and Google credentials and is written with
mode 0600. Environment overrides (`/etc/default/mariadb-backup-manager`, or
`-e` in Docker): `MBM_PORT`, `MBM_LISTEN`, `MBM_DATA_DIR`,
`MBM_DEFAULT_BACKUP_DIR`, `MBM_WORK_DIR` (where backups are staged before they
are stored; needs room for two compressed backups), `MBM_ADMIN_USER` /
`MBM_ADMIN_PASSWORD` (first start only). Schedules run in the host's (or the
container's `TZ`) time zone.

The UI has a single administrator login. Put it behind TLS (a reverse proxy)
if it is reachable from anywhere you do not trust.

## Development

```bash
python3 -m unittest discover -s tests -p 'test_*.py'      # no database needed
MBM_DB_USER=root tests/integration.sh                     # full round trip on a real server
MBM_DATA_DIR=/tmp/mbm MBM_ADMIN_PASSWORD=testpass123 python3 app.py
node tests/e2e.js                                         # the UI, driven by Playwright
```

CI runs all three against MariaDB 10.11 and 11.8, builds and installs the
`.deb`/`.rpm` on Debian, Ubuntu, Rocky and Fedora, and builds the
multi-architecture image.

## License

GPL-3.0. The Proxmox scripts are MIT, like the community-scripts engine they
build on.
