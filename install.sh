#!/usr/bin/env bash
# MariaDB Backup Manager -- install, update or remove on a Debian-based server
# (Debian 12/13, Ubuntu 22.04/24.04).
#
#   curl -fsSL https://raw.githubusercontent.com/avandeputte/mariadb-backup/main/install.sh | sudo bash
#   sudo ./install.sh                 # from a checkout: installs these files
#   sudo ./install.sh --update        # or --remove, --purge, without asking
#
# Same layout as the .deb: /opt/mariadb-backup-manager, settings in
# /var/lib/mariadb-backup-manager, local backups in /var/backups/mariadb,
# systemd unit mariadb-backup-manager. Settings and backups survive updates.
set -euo pipefail

REPO="${MBM_REPO:-avandeputte/mariadb-backup}"
REF="${MBM_REF:-main}"
APP=/opt/mariadb-backup-manager
DATA=/var/lib/mariadb-backup-manager
BACKUPS=/var/backups/mariadb
UNIT=/etc/systemd/system/mariadb-backup-manager.service
PORT="${MBM_PORT:-8090}"

say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run as root (sudo)."
command -v apt-get >/dev/null || die "This script is for Debian-based systems; use the .rpm on RHEL/Fedora."
if dpkg -s mariadb-backup-manager >/dev/null 2>&1; then
  die "Installed from the .deb package; update with apt (or the Proxmox script), not this script."
fi

ACTION="${1:-}"
if [ -z "$ACTION" ] && [ -f "$APP/app.py" ]; then
  if [ -t 0 ] || [ -e /dev/tty ]; then
    echo "MariaDB Backup Manager is already installed ($(cat "$APP/VERSION" 2>/dev/null || echo '?'))."
    echo "  1) update   2) remove (keep settings and backups)   3) purge settings too   4) cancel"
    read -r -p "Choice [1]: " c </dev/tty || c=1
    case "${c:-1}" in 1) ACTION=--update ;; 2) ACTION=--remove ;; 3) ACTION=--purge ;; *) exit 0 ;; esac
  else
    ACTION=--update
    say "Already installed and no terminal to ask on: updating in place."
  fi
fi

remove() {
  systemctl disable --now mariadb-backup-manager >/dev/null 2>&1 || true
  rm -f "$UNIT"; systemctl daemon-reload || true
  rm -rf "$APP"
  if [ "$1" = purge ]; then rm -rf "$DATA"; say "Removed, settings deleted. Backups in $BACKUPS were left alone."
  else say "Removed. Settings kept in $DATA, backups in $BACKUPS."; fi
}
case "$ACTION" in
  --remove) remove keep; exit 0 ;;
  --purge)  remove purge; exit 0 ;;
  ""|--update|--install) ;;
  *) die "Unknown option $ACTION (use --update, --remove or --purge)" ;;
esac

say "Installing dependencies"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q >/dev/null
apt-get install -y -q --no-install-recommends python3 python3-flask python3-requests python3-waitress \
  mariadb-client openssl curl ca-certificates tar >/dev/null

SRC="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
TMP=""
if [ -z "$SRC" ] || [ ! -f "$SRC/app.py" ] || [ ! -d "$SRC/mbm" ]; then
  TMP="$(mktemp -d)"
  say "Downloading $REPO@$REF"
  curl -fsSL "https://codeload.github.com/$REPO/tar.gz/$REF" | tar -xz -C "$TMP" --strip-components=1
  SRC="$TMP"
fi

say "Installing to $APP"
systemctl stop mariadb-backup-manager >/dev/null 2>&1 || true
install -d -m 0755 "$APP" "$APP/mbm"
install -d -m 0700 "$DATA" "$BACKUPS"
rm -rf "$APP/static" "$APP/mbm"/*.py
install -m 0644 "$SRC/app.py" "$SRC/VERSION" "$APP/"
install -m 0644 "$SRC"/mbm/*.py "$SRC/mbm/FILES" "$APP/mbm/"
cp -r "$SRC/static" "$APP/static"
install -m 0755 "$SRC/install.sh" "$APP/install.sh"
sed -e "s/^Environment=MBM_PORT=.*/Environment=MBM_PORT=$PORT/" \
    "$SRC/packaging/mariadb-backup-manager.service" > "$UNIT"
[ -n "$TMP" ] && rm -rf "$TMP"

SEEDED=""
if ! MBM_DATA_DIR="$DATA" python3 "$APP/app.py" show-admin 2>/dev/null | grep -qv '^(none)$'; then
  PW="$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  printf '%s' "$PW" | MBM_DATA_DIR="$DATA" python3 "$APP/app.py" set-admin admin - >/dev/null
  (umask 077; printf 'username: admin\npassword: %s\n' "$PW" > "$DATA/admin-credentials.txt")
  SEEDED=1
fi

systemctl daemon-reload
systemctl enable --now mariadb-backup-manager >/dev/null
for _ in $(seq 20); do curl -fs -o /dev/null "http://127.0.0.1:$PORT/api/whoami" && break; sleep 1; done
curl -fs -o /dev/null "http://127.0.0.1:$PORT/api/whoami" || warn "The service did not answer yet: journalctl -u mariadb-backup-manager"

ADDR="$(hostname -I 2>/dev/null | awk '{print $1}')"
say "MariaDB Backup Manager $(cat "$APP/VERSION") is running at http://${ADDR:-localhost}:$PORT"
if [ -n "$SEEDED" ]; then
  echo "    username  admin"
  echo "    password  $PW"
  echo "    (also in $DATA/admin-credentials.txt)"
fi
