#!/bin/sh
# First start: if MBM_ADMIN_PASSWORD is set the app creates the login itself;
# otherwise the UI asks for one on the first visit.
set -e
mkdir -p "$MBM_DATA_DIR" "$MBM_DEFAULT_BACKUP_DIR"
chmod 700 "$MBM_DATA_DIR"
exec "$@"
