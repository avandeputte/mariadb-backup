#!/bin/sh
# Runs after install and after upgrade, on both .deb and .rpm. Idempotent.
set -e
trap 'rc=$?; [ "$rc" -ne 0 ] && echo "MariaDB Backup Manager: post-install failed at line $LINENO (exit $rc); run: systemctl enable --now mariadb-backup-manager" >&2; exit $rc' EXIT

DATA=/var/lib/mariadb-backup-manager
install -d -m 0700 "$DATA"
install -d -m 0700 /var/backups/mariadb

# Seed the login BEFORE the service starts, so nobody else can claim the
# first-visit setup page on a fresh install.
if [ ! -f "$DATA/config.json" ] || ! /usr/bin/python3 /opt/mariadb-backup-manager/app.py show-admin 2>/dev/null | grep -qv '^(none)$'; then
    PW="$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    if printf '%s' "$PW" | MBM_DATA_DIR="$DATA" /usr/bin/python3 /opt/mariadb-backup-manager/app.py set-admin admin - >/dev/null 2>&1; then
        umask 077
        printf 'username: admin\npassword: %s\n' "$PW" > "$DATA/admin-credentials.txt"
        SEEDED=1
    fi
fi

systemctl daemon-reload >/dev/null 2>&1 || true
systemctl enable mariadb-backup-manager >/dev/null 2>&1 || true
systemctl restart mariadb-backup-manager >/dev/null 2>&1 || true

if [ -n "${SEEDED:-}" ]; then
    ADDR="$(hostname -I 2>/dev/null | awk '{print $1}')"
    [ -n "$ADDR" ] || ADDR="$(hostname)"
    cat <<EOF

  MariaDB Backup Manager is running at  http://$ADDR:8090

    username  admin
    password  $PW

  Also saved to $DATA/admin-credentials.txt (mode 0600).
  Change it under Settings, and put the UI behind TLS.

EOF
fi
exit 0
