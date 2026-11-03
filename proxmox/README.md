# Proxmox VE LXC

A one-line install of MariaDB Backup Manager into a Debian LXC on Proxmox VE,
built on the [community-scripts](https://community-scripts.org) engine. Run it
on the Proxmox host:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/avandeputte/mariadb-backup/main/proxmox/ct/mariadb-backup-manager.sh)"
```

It creates an **unprivileged** Debian 13 container (1 CPU, 512 MB, 8 GB disk by
default; the advanced-settings dialog changes any of it), installs the current
release `.deb`, starts the service and prints the URL and the generated login.
The login is also kept in `/root/mariadb-backup-manager.creds` in the container.

## Disk

Backups are staged in the container before they are stored, and a local
destination keeps them there. Size the disk for at least two backups plus the
number you keep, or point a local destination at a bind mount
(`pct set <CTID> -mp0 /tank/mariadb-backups,mp=/backups`) or use Google Drive.

## Updating

Run the same script **inside** the container; the engine notices and updates
instead of building a new one:

```bash
pct enter <CTID>
bash -c "$(curl -fsSL https://raw.githubusercontent.com/avandeputte/mariadb-backup/main/proxmox/ct/mariadb-backup-manager.sh)"
```

The two scripts follow the community-scripts CT and install templates and are
MIT-licensed like the engine they build on, which is why their headers carry
both copyrights. They live here until the project meets the community-scripts
listing criteria.
