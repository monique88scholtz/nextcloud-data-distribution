#!/usr/bin/env bash
# =============================================================
#  backup.sh
#  AUTOMATED NEXTCLOUD BACKUP
#  ─────────────────────────────────────────────────────────────
#  Backs up:
#    - Nextcloud database (MariaDB dump)
#    - Nextcloud config files
#    - NOT the data files (those live on /mnt/data already)
#
#  Retention: keeps last 30 days of backups
#
#  Usage:
#    sudo bash scripts/backup.sh
#
#  Add to cron (runs at 2am daily):
#    0 2 * * * cd /opt/nextcloud-setup/nextcloud-data-distribution && \
#      sudo bash scripts/backup.sh >> logs/backup.log 2>&1
# =============================================================

set -euo pipefail

# ── Config ────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/.."
ENV_FILE="${SCRIPT_DIR}/config/.env"
BACKUP_DIR="${SCRIPT_DIR}/backups"
RETENTION_DAYS=30
NC_PATH="/var/www/html/nextcloud"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")

# ── Colours ───────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'
CYAN='\033[0;36m';  BOLD='\033[1m';      NC='\033[0m'
info()  { echo -e "${CYAN}[$(date '+%Y-%m-%d %H:%M:%S')][INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[$(date '+%Y-%m-%d %H:%M:%S')][OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[$(date '+%Y-%m-%d %H:%M:%S')][WARN]${NC}  $*"; }
error() { echo -e "${RED}[$(date '+%Y-%m-%d %H:%M:%S')][ERROR]${NC} $*"; exit 1; }

# ── Load env ──────────────────────────────────────────────────
[[ ! -f "$ENV_FILE" ]] && error ".env not found at $ENV_FILE"
source "$ENV_FILE"

# ── Setup ─────────────────────────────────────────────────────
mkdir -p "${BACKUP_DIR}"
BACKUP_PATH="${BACKUP_DIR}/backup_${TIMESTAMP}"
mkdir -p "${BACKUP_PATH}"

info "Starting backup → ${BACKUP_PATH}"

# ── Enable maintenance mode ────────────────────────────────────
info "Enabling maintenance mode..."
sudo -u www-data php "${NC_PATH}/occ" maintenance:mode --on
ok "Maintenance mode ON"

# ── Database backup ───────────────────────────────────────────
info "Backing up database..."
mysqldump \
    -u "${DB_USER}" \
    -p"${DB_PASSWORD}" \
    --single-transaction \
    --quick \
    --lock-tables=false \
    "${DB_NAME}" > "${BACKUP_PATH}/nextcloud_db_${TIMESTAMP}.sql"

gzip "${BACKUP_PATH}/nextcloud_db_${TIMESTAMP}.sql"
ok "Database backed up → nextcloud_db_${TIMESTAMP}.sql.gz"

# ── Config backup ─────────────────────────────────────────────
info "Backing up Nextcloud config..."
tar -czf "${BACKUP_PATH}/nextcloud_config_${TIMESTAMP}.tar.gz" \
    -C "${NC_PATH}" config/
ok "Config backed up → nextcloud_config_${TIMESTAMP}.tar.gz"

# ── Project backup ────────────────────────────────────────────
info "Backing up project files..."
tar -czf "${BACKUP_PATH}/project_${TIMESTAMP}.tar.gz" \
    --exclude="${SCRIPT_DIR}/backups" \
    --exclude="${SCRIPT_DIR}/.venv" \
    --exclude="${SCRIPT_DIR}/.git" \
    -C "$(dirname ${SCRIPT_DIR})" \
    "$(basename ${SCRIPT_DIR})"
ok "Project backed up → project_${TIMESTAMP}.tar.gz"

# ── Disable maintenance mode ──────────────────────────────────
sudo -u www-data php "${NC_PATH}/occ" maintenance:mode --off
ok "Maintenance mode OFF"

# ── Retention — delete backups older than N days ──────────────
info "Cleaning up backups older than ${RETENTION_DAYS} days..."
find "${BACKUP_DIR}" -maxdepth 1 -type d -mtime +${RETENTION_DAYS} -exec rm -rf {} + 2>/dev/null || true
ok "Cleanup done"

# ── Summary ───────────────────────────────────────────────────
BACKUP_SIZE=$(du -sh "${BACKUP_PATH}" | cut -f1)
echo ""
echo -e "${BOLD}${GREEN}━━━  Backup Complete  ━━━${NC}"
echo -e "  Location : ${BACKUP_PATH}"
echo -e "  Size     : ${BACKUP_SIZE}"
echo -e "  Time     : $(date '+%Y-%m-%d %H:%M:%S')"
echo -e "  Retention: ${RETENTION_DAYS} days"
echo ""
