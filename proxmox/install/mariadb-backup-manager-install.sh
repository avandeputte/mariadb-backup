#!/usr/bin/env bash

# Copyright (c) 2021-2026 community-scripts ORG -- Core engine and script template (MIT)
# Copyright (c) 2026 Alex Van de Putte (avandeputte)
# License: MIT | https://github.com/community-scripts/core/blob/main/LICENSE
# Source: https://github.com/avandeputte/mariadb-backup
#
# Runs inside the container the ct/ script built. Installs MariaDB Backup
# Manager from its release .deb, which pulls the MariaDB client and the
# Python dependencies through Depends.

source /dev/stdin <<<"$FUNCTIONS_FILE_PATH"
color
verb_ip6
catch_errors
setting_up_container
network_check
update_os

fetch_and_deploy_gh_release "mariadb-backup-manager" "avandeputte/mariadb-backup" "binary" "latest" "/opt/mariadb-backup-manager" "mariadb-backup-manager_*_all.deb"

msg_info "Starting MariaDB Backup Manager"
# The package is installed under SYSTEMD_OFFLINE, so its own restart is a
# no-op here; start it explicitly and wait for the UI to answer.
systemctl enable -q --now mariadb-backup-manager
for _ in {1..30}; do
  curl -fs -o /dev/null http://127.0.0.1:8090/api/whoami && break
  sleep 2
done
msg_ok "Started MariaDB Backup Manager"

msg_info "Saving Credentials"
cat /var/lib/mariadb-backup-manager/admin-credentials.txt >>~/mariadb-backup-manager.creds
chmod 600 ~/mariadb-backup-manager.creds
msg_ok "Saved Credentials to ~/mariadb-backup-manager.creds"

motd_ssh
customize
cleanup_lxc
