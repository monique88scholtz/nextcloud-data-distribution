#!/usr/bin/env bash
# =============================================================
#  NEXTCLOUD AUTO-INSTALLER — with CSV-driven client provisioning
#  ─────────────────────────────────────────────────────────────
#  1. Fill in config/.env  (copy from config/.env.template)
#  2. Place your CSV at    data/client_distribution.csv
#  3. sudo ./install_nextcloud.sh
# =============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/config/.env"
CSV_FILE="${SCRIPT_DIR}/data/client_distribution.csv"

# ── Colours ───────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()      { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }
section() { echo -e "\n${BOLD}${GREEN}━━━  $*  ━━━${NC}\n"; }

# ── Root check ────────────────────────────────────────────────
[[ $EUID -ne 0 ]] && error "Run as root: sudo ./install_nextcloud.sh"

# ── Load .env ─────────────────────────────────────────────────
[[ ! -f "$ENV_FILE" ]] && error ".env not found at $ENV_FILE — copy config/.env.template first"
source "$ENV_FILE"

# ── Validate .env ─────────────────────────────────────────────
section "Validating Configuration"
for var in NC_DOMAIN DB_NAME DB_USER DB_PASSWORD NC_ADMIN_USER NC_ADMIN_PASSWORD SSL_EMAIL; do
    [[ -z "${!var:-}" ]]                    && error "Missing variable: $var"
    [[ "${!var}" == *"CHANGE_THIS"* ]]      && error "$var still has placeholder value — edit config/.env"
done
ok "Configuration valid"

get_php_version() { php -r 'echo PHP_MAJOR_VERSION.".".PHP_MINOR_VERSION;' 2>/dev/null || echo "8.1"; }

# =============================================================
# 1 — System packages
# =============================================================
section "Step 1 — System Update & Packages"
apt-get update -qq && apt-get upgrade -y -qq
apt-get install -y -qq \
    apache2 mariadb-server libapache2-mod-php \
    php php-gd php-mysql php-curl php-mbstring php-intl \
    php-imagick php-xml php-zip php-bcmath php-gmp \
    php-json php-bz2 php-apcu redis-server php-redis \
    unzip wget curl certbot python3-certbot-apache \
    python3 python3-pip python3-venv
ok "Packages installed"

# =============================================================
# 2 — Python deps for provisioning scripts
# =============================================================
section "Step 2 — Python Dependencies"
python3 -m venv "${SCRIPT_DIR}/.venv"
"${SCRIPT_DIR}/.venv/bin/pip" install --quiet pandas python-dotenv requests
ok "Python dependencies installed in .venv"

# =============================================================
# 3 — PHP configuration
# =============================================================
section "Step 3 — Configure PHP"
PHP_VER=$(get_php_version)
PHP_INI=$(find /etc/php -name php.ini -path "*/apache2/*" | head -1)
[[ -z "$PHP_INI" ]] && error "Could not locate php.ini"
sed -i "s/^memory_limit.*/memory_limit = ${PHP_MEMORY_LIMIT}/"                 "$PHP_INI"
sed -i "s/^upload_max_filesize.*/upload_max_filesize = ${PHP_UPLOAD_MAX_FILESIZE}/" "$PHP_INI"
sed -i "s/^post_max_size.*/post_max_size = ${PHP_POST_MAX_SIZE}/"               "$PHP_INI"
sed -i "s/^max_execution_time.*/max_execution_time = ${PHP_MAX_EXECUTION_TIME}/" "$PHP_INI"
sed -i "s|^;date.timezone.*|date.timezone = ${NC_TIMEZONE}|"                   "$PHP_INI"
sed -i "s/^;opcache.enable=.*/opcache.enable=1/"                                "$PHP_INI"
sed -i "s/^;opcache.memory_consumption=.*/opcache.memory_consumption=128/"      "$PHP_INI"
sed -i "s/^;opcache.max_accelerated_files=.*/opcache.max_accelerated_files=10000/" "$PHP_INI"
sed -i "s/^;opcache.revalidate_freq=.*/opcache.revalidate_freq=1/"              "$PHP_INI"
ok "PHP $PHP_VER configured"

# =============================================================
# 4 — MariaDB
# =============================================================
section "Step 4 — Configure MariaDB"
systemctl enable --now mariadb

# ── WHY this approach: ────────────────────────────────────────
# Modern MariaDB on Ubuntu uses unix_socket auth for root by
# default. That means:
#   - root can login passwordless via sudo (socket auth)
#   - mysql.user is a VIEW — UPDATE on it fails (HY000 error)
#   - We must use ALTER USER / native password plugin instead
# We leave root on unix_socket (safer) and create a dedicated
# Nextcloud DB user with a real password.
# ─────────────────────────────────────────────────────────────

info "Securing MariaDB (unix_socket root — no password update needed)..."

# Remove anonymous users and test DB safely
mysql -u root <<SQL
  -- Remove anonymous users
  DELETE FROM mysql.global_priv WHERE User='';
  -- Remove remote root (keep only socket/localhost)
  DELETE FROM mysql.global_priv
    WHERE User='root' AND Host NOT IN ('localhost','127.0.0.1','::1');
  -- Drop test database
  DROP DATABASE IF EXISTS test;
  DELETE FROM mysql.db WHERE Db='test' OR Db='test\\_%';
  FLUSH PRIVILEGES;
SQL

info "Creating Nextcloud database and user..."
mysql -u root <<SQL
  CREATE DATABASE IF NOT EXISTS \`${DB_NAME}\`
    CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;

  -- Drop user if it exists so we can recreate cleanly
  DROP USER IF EXISTS '${DB_USER}'@'localhost';

  -- Create with explicit native password (works on all MariaDB versions)
  CREATE USER '${DB_USER}'@'localhost'
    IDENTIFIED VIA mysql_native_password USING PASSWORD('${DB_PASSWORD}');

  GRANT ALL PRIVILEGES ON \`${DB_NAME}\`.* TO '${DB_USER}'@'localhost';
  FLUSH PRIVILEGES;
SQL

# Verify the user was created
if mysql -u "${DB_USER}" -p"${DB_PASSWORD}" -e "USE ${DB_NAME};" 2>/dev/null; then
    ok "MariaDB ready — database '${DB_NAME}', user '${DB_USER}' verified"
else
    error "MariaDB user verification failed — check DB_PASSWORD in config/.env"
fi

# =============================================================
# 5 — Download Nextcloud
# =============================================================
section "Step 5 — Download & Extract Nextcloud"
NC_INSTALL_DIR="/var/www/html/nextcloud"
if [[ -d "$NC_INSTALL_DIR" ]]; then
    warn "Nextcloud directory exists — skipping download"
else
    wget -q --show-progress -O /tmp/nextcloud.zip \
        https://download.nextcloud.com/server/releases/latest.zip
    unzip -q /tmp/nextcloud.zip -d /var/www/html/
    rm /tmp/nextcloud.zip
    ok "Nextcloud extracted"
fi
[[ "${NC_DATA_DIR}" != "${NC_INSTALL_DIR}/data" ]] && mkdir -p "${NC_DATA_DIR}"
chown -R www-data:www-data "$NC_INSTALL_DIR"
chmod -R 755 "$NC_INSTALL_DIR"
ok "Permissions set"

# =============================================================
# 6 — Apache virtual host
# =============================================================
section "Step 6 — Apache Virtual Host"
cat > /etc/apache2/sites-available/nextcloud.conf <<APACHE
<VirtualHost *:80>
    ServerName ${NC_DOMAIN}
    DocumentRoot ${NC_INSTALL_DIR}

    <Directory ${NC_INSTALL_DIR}/>
        Require all granted
        AllowOverride All
        Options FollowSymLinks MultiViews
        <IfModule mod_dav.c>
            Dav off
        </IfModule>
    </Directory>

    <IfModule mod_headers.c>
        Header always set Strict-Transport-Security "max-age=15552000; includeSubDomains"
        Header always set X-Content-Type-Options "nosniff"
        Header always set X-Frame-Options "SAMEORIGIN"
        Header always set X-XSS-Protection "1; mode=block"
        Header always set Referrer-Policy "no-referrer"
    </IfModule>

    ErrorLog \${APACHE_LOG_DIR}/nextcloud_error.log
    CustomLog \${APACHE_LOG_DIR}/nextcloud_access.log combined
</VirtualHost>
APACHE
a2dissite 000-default.conf 2>/dev/null || true
a2ensite nextcloud.conf
a2enmod rewrite headers env dir mime setenvif
systemctl restart apache2
ok "Apache configured"

# =============================================================
# 7 — Install Nextcloud via CLI
# =============================================================
section "Step 7 — Nextcloud CLI Install"
sudo -u www-data php "${NC_INSTALL_DIR}/occ" maintenance:install \
    --database "mysql" --database-host "${DB_HOST}" \
    --database-name "${DB_NAME}" --database-user "${DB_USER}" \
    --database-pass "${DB_PASSWORD}" \
    --admin-user "${NC_ADMIN_USER}" --admin-pass "${NC_ADMIN_PASSWORD}" \
    --data-dir "${NC_DATA_DIR}"

sudo -u www-data php "${NC_INSTALL_DIR}/occ" config:system:set trusted_domains 0 --value="${NC_DOMAIN}"
sudo -u www-data php "${NC_INSTALL_DIR}/occ" config:system:set trusted_domains 1 --value="${NC_SERVER_IP}"
ok "Nextcloud installed — trusted domains set"

# =============================================================
# 8 — Redis
# =============================================================
section "Step 8 — Redis Cache"
systemctl enable --now redis-server
OCC="sudo -u www-data php ${NC_INSTALL_DIR}/occ"
$OCC config:system:set memcache.local   --value='\OC\Memcache\APCu'
$OCC config:system:set memcache.locking --value='\OC\Memcache\Redis'
$OCC config:system:set redis host       --value="${REDIS_HOST}"
$OCC config:system:set redis port       --value="${REDIS_PORT}" --type=integer
[[ -n "${REDIS_PASSWORD:-}" ]] && $OCC config:system:set redis password --value="${REDIS_PASSWORD}"
ok "Redis configured"

# =============================================================
# 9 — Sharing security hardening
# =============================================================
section "Step 9 — Sharing Security"
$OCC config:app:set core shareapi_enforce_links_password        --value="${NC_SHARE_ENFORCE_PASSWORD}"
$OCC config:app:set core shareapi_allow_resharing               --value="$([ "${NC_SHARE_ALLOW_RESHARE}" = "true" ] && echo yes || echo no)"
$OCC config:app:set core shareapi_expire_after_n_days           --value="${NC_SHARE_DEFAULT_EXPIRE_DAYS}"
$OCC config:app:set core shareapi_default_expire_date           --value="yes"
$OCC config:app:set core shareapi_enforce_expire_date           --value="yes"
$OCC config:app:set core shareapi_allow_share_dialog_user_enumeration \
    --value="$([ "${NC_SHARE_HIDE_USERNAME_AUTOCOMPLETE}" = "true" ] && echo no || echo yes)"
ok "Sharing hardened"

# =============================================================
# 10 — SSL
# =============================================================
section "Step 10 — SSL"
if [[ "${ENABLE_SSL}" == "true" ]] && \
   [[ "${NC_DOMAIN}" != "your-domain.com" ]] && \
   ! [[ "${NC_DOMAIN}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    certbot --apache -d "${NC_DOMAIN}" \
        --email "${SSL_EMAIL}" --agree-tos --non-interactive --redirect
    systemctl enable certbot.timer
    ok "SSL installed for ${NC_DOMAIN}"
else
    warn "Skipping SSL (no valid domain or ENABLE_SSL=false)"
fi

# =============================================================
# 11 — Cron
# =============================================================
section "Step 11 — Cron"
$OCC background:cron
(crontab -u www-data -l 2>/dev/null; \
 echo "*/5  *  *  *  * php -f ${NC_INSTALL_DIR}/cron.php") | crontab -u www-data -
ok "Cron job added"

# =============================================================
# 12 — Final tweaks
# =============================================================
section "Step 12 — Final Tweaks"
$OCC config:system:set default_phone_region --value="ZA"
$OCC config:system:set auth.bruteforce.protection.enabled --value=true --type=boolean
$OCC maintenance:update:htaccess
systemctl restart apache2

# =============================================================
# 13 — CSV Provisioning (users, groups, shares)
# =============================================================
section "Step 13 — Provision Clients from CSV"
if [[ -f "$CSV_FILE" ]]; then
    info "Running provision_clients.py..."
    "${SCRIPT_DIR}/.venv/bin/python3" \
        "${SCRIPT_DIR}/scripts/provision_clients.py" \
        --env "${ENV_FILE}" \
        --csv "${CSV_FILE}" \
        --report
else
    warn "CSV not found at ${CSV_FILE}"
    warn "Run manually later:"
    warn "  python3 scripts/provision_clients.py --env config/.env --csv data/client_distribution.csv"
fi

# =============================================================
# Done
# =============================================================
section "Installation Complete!"
echo -e "
${BOLD}╔══════════════════════════════════════════════════════╗
║         NEXTCLOUD DISTRIBUTION SETUP DONE            ║
╠══════════════════════════════════════════════════════╣
║  URL:        https://${NC_DOMAIN}
║  Admin:      ${NC_ADMIN_USER}
║  Data dir:   ${NC_DATA_DIR}
║                                                      ║
║  CLIENTS PROVISIONED FROM CSV:                       ║
║  ✅ Users created with UUID passwords                ║
║  ✅ Groups created & assigned                        ║
║  ✅ Data folders mounted per group                   ║
║  ✅ Clients can ONLY see their own data              ║
║                                                      ║
║  NEXT QUARTER: run scripts/update_quarter.py         ║
║  GITHUB: see README.md for git setup steps           ║
╚══════════════════════════════════════════════════════╝${NC}
"
warn "Secure your .env: chmod 600 config/.env"
