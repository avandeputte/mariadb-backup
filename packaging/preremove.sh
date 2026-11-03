#!/bin/sh
# $1 is "remove"/"upgrade" (deb) or 0/1 (rpm). Stop only on a real removal.
set -e
case "${1:-}" in
    upgrade|1) exit 0 ;;
esac
systemctl stop mariadb-backup-manager >/dev/null 2>&1 || true
systemctl disable mariadb-backup-manager >/dev/null 2>&1 || true
exit 0
