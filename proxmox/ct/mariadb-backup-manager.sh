#!/usr/bin/env bash
# The scripts root: where the engine looks for install/mariadb-backup-manager-install.sh.
export COMMUNITY_SCRIPTS_URL="${COMMUNITY_SCRIPTS_URL:-https://raw.githubusercontent.com/avandeputte/mariadb-backup/main/proxmox}"
_cs_boot="${COMMUNITY_SCRIPTS_CORE_DIR:-$(dirname "${BASH_SOURCE[0]}")/../../core}/core/build.func"
source "$_cs_boot" 2>/dev/null || source <(curl -fsSL "${COMMUNITY_SCRIPTS_CORE_URL:-https://raw.githubusercontent.com/community-scripts/core/main}/core/build.func")
# Copyright (c) 2021-2026 community-scripts ORG -- Core engine and script template (MIT)
# Copyright (c) 2026 Alex Van de Putte (avandeputte)
# License: MIT | https://github.com/community-scripts/core/blob/main/LICENSE
# Source: https://github.com/avandeputte/mariadb-backup
#
# Builds a Proxmox VE LXC running MariaDB Backup Manager, using the
# community-scripts engine. See proxmox/README.md.

APP="MariaDB-Backup-Manager"
var_tags="${var_tags:-database;backup;mariadb}"
var_cpu="${var_cpu:-1}"
var_ram="${var_ram:-512}"
var_disk="${var_disk:-8}"
var_os="${var_os:-debian}"
var_version="${var_version:-13}"
var_unprivileged="${var_unprivileged:-1}"
var_arm64="${var_arm64:-yes}"

header_info "$APP"
variables
color
catch_errors

function update_script() {
  header_info
  check_container_storage
  check_container_resources

  if [[ ! -f /opt/mariadb-backup-manager/app.py ]]; then
    msg_error "No ${APP} Installation Found!"
    exit
  fi

  if check_for_gh_release "mariadb-backup-manager" "avandeputte/mariadb-backup"; then
    msg_info "Stopping Service"
    systemctl stop mariadb-backup-manager
    msg_ok "Stopped Service"

    fetch_and_deploy_gh_release "mariadb-backup-manager" "avandeputte/mariadb-backup" "binary" "latest" "/opt/mariadb-backup-manager" "mariadb-backup-manager_*_all.deb"

    msg_info "Starting Service"
    systemctl daemon-reload
    systemctl start mariadb-backup-manager
    msg_ok "Started Service"
    msg_ok "Updated successfully!"
  fi
  exit
}

start
build_container
description

msg_ok "Completed Successfully!\n"
echo -e "${CREATING}${GN}${APP} setup has been successfully initialized!${CL}"
echo -e "${INFO}${YW} Access it using the following URL:${CL}"
echo -e "${TAB}${GATEWAY}${BGN}http://${IP}:8090${CL}"
echo -e "${INFO}${YW} Credentials (also in /root/mariadb-backup-manager.creds in the container):${CL}"
echo -e "${TAB}${DGN}$(pct exec "$CTID" -- cat /root/mariadb-backup-manager.creds)${CL}"
