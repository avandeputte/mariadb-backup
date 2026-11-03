#!/bin/sh
# Settings are kept on remove; `apt-get purge` clears them. Backups in
# /var/backups/mariadb (or wherever you pointed a destination) are never
# deleted by the package: they are the whole point.
set -e
systemctl daemon-reload >/dev/null 2>&1 || true
if [ "${1:-}" = "purge" ]; then
    rm -rf /var/lib/mariadb-backup-manager
fi
exit 0
