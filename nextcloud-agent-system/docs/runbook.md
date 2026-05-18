# 📘 Nextcloud Data Distribution — Operations Runbook

> **Who this is for:** Anyone managing this system — including future you at a new job.  
> **What it covers:** Day-to-day operations, quarterly updates, agent management, and troubleshooting.  
> **Skill level assumed:** Junior data engineer comfortable with SSH and basic Linux.

---

## 🗺️ System Overview

```
┌─────────────────────────────────────────────────┐
│  Ubuntu Server                                   │
│                                                  │
│  ┌──────────┐  ┌──────────┐  ┌──────────────┐  │
│  │ Apache 2 │  │ MariaDB  │  │    Redis     │  │
│  │  (HTTP)  │  │   (DB)   │  │   (Cache)    │  │
│  └──────────┘  └──────────┘  └──────────────┘  │
│                                                  │
│  ┌────────────────────────────────────────────┐ │
│  │            Nextcloud                        │ │
│  │  Users → Groups → External Storage Mounts  │ │
│  └────────────────────────────────────────────┘ │
│                                                  │
│  ┌────────────────────────────────────────────┐ │
│  │  Agents                                     │ │
│  │  agent_provision.py  — user/group sync      │ │
│  │  agent_refresh.py    — file cache refresh   │ │
│  └────────────────────────────────────────────┘ │
│                                                  │
│  /mnt/data/DISTRIBUTION_QX_YYYY/               │
│  ├── MN/        ├── MNR/      ├── MAPIT/        │
│  └── PRODUCTS/                                  │
└─────────────────────────────────────────────────┘
```

### How Access Control Works

```
CSV Row
  └── CLIENT (e.g. Esri)
        └── GROUP (e.g. mnr_afr)
              └── MOUNT (e.g. /MNR_MEA → /mnt/data/.../MNR_MEA)
                    └── Client logs in → sees only their mounts
```

No client can see another client's data. Each mount is scoped to exactly one group.

---

## 📁 Project Structure

```
/opt/nextcloud-setup/nextcloud-data-distribution/
├── install_nextcloud.sh          # Full server installer (run once)
├── config/
│   ├── .env                      # Your secrets (NEVER commit to git)
│   ├── .env.template             # Safe template for git
│   └── nextcloud-refresh.service # Systemd service definition
├── data/
│   ├── client_distribution.csv  # Current quarter CSV
│   └── prev_quarter.csv         # Previous quarter (for diff)
├── agents/
│   ├── agent_provision.py        # User/group provisioning agent
│   └── agent_refresh.py          # File cache refresh agent
├── scripts/
│   ├── provision_clients.py      # One-shot provisioner
│   ├── update_quarter.py         # Quarter update script
│   └── validate_csv.py           # CSV validator
├── logs/                         # Agent log files
└── docs/
    ├── runbook.md                # This file
    └── architecture.md           # System design
```

---

## 🔄 Quarterly Update Process

This is the most common task you'll do — every quarter when new data arrives.

### Step 1 — Prepare the new CSV

```bash
# Save current CSV as previous quarter reference
cp data/client_distribution.csv data/prev_quarter.csv

# Copy in the new CSV
cp /path/to/new/Q2_2026_Distribution.csv data/client_distribution.csv
```

### Step 2 — Validate before touching anything

```bash
cd /opt/nextcloud-setup/nextcloud-data-distribution
.venv/bin/python3 scripts/validate_csv.py --csv data/client_distribution.csv
```

Fix any errors it reports before proceeding.

### Step 3 — Preview changes (dry run)

```bash
sudo .venv/bin/python3 scripts/update_quarter.py \
    --env config/.env \
    --old data/prev_quarter.csv \
    --new data/client_distribution.csv \
    --dry-run
```

Review the output carefully:
- **New clients** → will be created
- **Removed clients** → will be disabled (not deleted)
- **Group changes** → access added or removed

### Step 4 — Apply changes

```bash
sudo .venv/bin/python3 scripts/update_quarter.py \
    --env config/.env \
    --old data/prev_quarter.csv \
    --new data/client_distribution.csv
```

### Step 5 — Mount any new data folders

If new `DirectoryPath` entries were added, mount them:

```bash
# Create the mount
sudo -u www-data php /var/www/html/nextcloud/occ files_external:create \
    "/FOLDER_NAME" local null::null \
    -c datadir="/mnt/data/path/to/folder"

# Get the mount ID
sudo -u www-data php /var/www/html/nextcloud/occ files_external:list | tail -5

# Scope to the correct group (replace ID and GROUP)
sudo -u www-data php /var/www/html/nextcloud/occ \
    files_external:applicable --remove-all ID
sudo -u www-data php /var/www/html/nextcloud/occ \
    files_external:applicable --add-group GROUP_NAME ID
```

### Step 6 — Trigger file scan

```bash
sudo -u www-data php /var/www/html/nextcloud/occ files:scan --all
```

### Step 7 — Commit to git

```bash
git add data/client_distribution.csv
git commit -m "data: Q2 2026 distribution update"
git push
```

---

## 🤖 Agent Management

### Refresh Agent (runs every minute)

The refresh agent keeps file listings up to date automatically.

**Install as a system service:**

```bash
# Copy service file
sudo cp config/nextcloud-refresh.service /etc/systemd/system/

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable nextcloud-refresh
sudo systemctl start nextcloud-refresh

# Check status
sudo systemctl status nextcloud-refresh

# View logs
tail -f logs/refresh.log
```

**Run manually (one-off):**

```bash
sudo .venv/bin/python3 agents/agent_refresh.py --env config/.env
```

**Run in watch mode (useful during testing):**

```bash
sudo .venv/bin/python3 agents/agent_refresh.py --env config/.env --daemon --interval 30
```

### Provision Agent (watches for CSV changes)

Drop a new CSV and it auto-provisions — no manual steps needed.

**Start watching:**

```bash
sudo .venv/bin/python3 agents/agent_provision.py \
    --env config/.env \
    --csv data/client_distribution.csv \
    --watch \
    --interval 60
```

**Run immediately:**

```bash
sudo .venv/bin/python3 agents/agent_provision.py \
    --env config/.env \
    --csv data/client_distribution.csv \
    --run-now
```

---

## 👥 Managing Users On the Fly

### Add a user to a group (give access to a dataset)

```bash
sudo -u www-data php /var/www/html/nextcloud/occ \
    group:adduser GROUP_NAME username
```

Example — give `esri` access to `mnr_eur`:
```bash
sudo -u www-data php /var/www/html/nextcloud/occ \
    group:adduser mnr_eur esri
```

### Remove a user from a group (revoke access)

```bash
sudo -u www-data php /var/www/html/nextcloud/occ \
    group:removeuser GROUP_NAME username
```

### Reset a user's password

```bash
export OC_PASS="new-password-here"
sudo -u www-data OC_PASS="new-password-here" php /var/www/html/nextcloud/occ \
    user:resetpassword --password-from-env username
```

### Disable a user (when a client contract ends)

```bash
sudo -u www-data php /var/www/html/nextcloud/occ user:disable username
```

### Re-enable a user

```bash
sudo -u www-data php /var/www/html/nextcloud/occ user:enable username
```

### List all users and their groups

```bash
sudo -u www-data php /var/www/html/nextcloud/occ user:list --output=json | python3 -m json.tool
```

### Check a specific user's access

```bash
sudo -u www-data php /var/www/html/nextcloud/occ user:info USERNAME --output=json | python3 -m json.tool
```

---

## 🗂️ Managing Mounts On the Fly

### List all mounts

```bash
sudo -u www-data php /var/www/html/nextcloud/occ files_external:list
```

### Add a new mount

```bash
sudo -u www-data php /var/www/html/nextcloud/occ files_external:create \
    "/MOUNT_NAME" local null::null \
    -c datadir="/mnt/data/path/to/folder"
```

### Scope a mount to a group

```bash
# Remove from All first
sudo -u www-data php /var/www/html/nextcloud/occ \
    files_external:applicable --remove-all MOUNT_ID

# Add to specific group
sudo -u www-data php /var/www/html/nextcloud/occ \
    files_external:applicable --add-group GROUP_NAME MOUNT_ID
```

### Delete a mount

```bash
sudo -u www-data php /var/www/html/nextcloud/occ files_external:delete MOUNT_ID
```

---

## 🔍 Troubleshooting

### Client says they can't see their data

```bash
# 1. Check their groups
sudo -u www-data php /var/www/html/nextcloud/occ user:info USERNAME --output=json | python3 -m json.tool

# 2. Check mounts for their groups
sudo -u www-data php /var/www/html/nextcloud/occ files_external:list

# 3. Force rescan for that user
sudo -u www-data php /var/www/html/nextcloud/occ files:scan USERNAME

# 4. Check the data path exists on disk
ls /mnt/data/DISTRIBUTION_QX_YYYY/path/to/their/folder
```

### 500 Internal Server Error

```bash
# Check Apache error log
tail -50 /var/log/apache2/nextcloud_error.log

# Check Nextcloud log
tail -30 /var/www/html/nextcloud/data/nextcloud.log

# Restart services
sudo systemctl restart apache2
sudo systemctl restart redis-server
```

### PHP version issues

```bash
# Check active PHP version
php --version

# Switch to PHP 8.3 if needed
sudo a2dismod php8.5
sudo a2enmod php8.3
sudo systemctl restart apache2
```

### MariaDB connection issues

```bash
# Test DB connection
mysql -u nextclouduser -p nextcloud -e "SELECT 1;"

# Check MariaDB status
sudo systemctl status mariadb
```

### Files not updating for clients

```bash
# Force full scan
sudo -u www-data php /var/www/html/nextcloud/occ files:scan --all

# Check refresh agent is running
sudo systemctl status nextcloud-refresh

# Check refresh logs
tail -50 logs/refresh.log
```

---

## 🔒 Security Checklist

Run this monthly:

```bash
# Check for failed login attempts
grep "Login failed" /var/www/html/nextcloud/data/nextcloud.log | tail -20

# Check firewall status
sudo ufw status

# Check for system updates
sudo apt list --upgradable 2>/dev/null | head -20

# Apply security updates
sudo apt update && sudo apt upgrade -y

# Check SSL certificate expiry (once you have a domain)
sudo certbot certificates
```

---

## 🌐 Adding a Domain (when DNS is ready)

```bash
# 1. Update .env
nano config/.env
# Set: NC_DOMAIN=content.yourdomain.com
# Set: ENABLE_SSL=true

# 2. Add domain to Nextcloud trusted domains
sudo -u www-data php /var/www/html/nextcloud/occ \
    config:system:set trusted_domains 0 --value="content.yourdomain.com"

# 3. Install SSL certificate
sudo certbot --apache -d content.yourdomain.com \
    --email your@email.com \
    --agree-tos --non-interactive --redirect

# 4. Enable auto-renewal
sudo systemctl enable certbot.timer
```

---

## 📊 Useful One-Liners

```bash
# How many files does each client have access to?
for user in $(sudo -u www-data php /var/www/html/nextcloud/occ user:list | grep -v admin | awk '{print $2}'); do
    count=$(sudo -u www-data php /var/www/html/nextcloud/occ files:scan $user 2>/dev/null | grep Files | awk '{print $4}')
    echo "$user: $count files"
done

# List all groups and their members
sudo -u www-data php /var/www/html/nextcloud/occ group:list --output=json | python3 -m json.tool

# Check disk usage of data directories
du -sh /mnt/data/DISTRIBUTION_Q1_2026/*

# Check server resource usage
htop
df -h
free -h
```

---

## 🚀 Skills Demonstrated in This Project

This project is a portfolio piece covering:

- **Linux server administration** — Ubuntu, Apache, MariaDB, Redis, systemd
- **Python automation** — pandas, subprocess, dotenv, file watching, agents
- **Data engineering** — CSV ingestion, data validation, ETL pipeline thinking
- **Security** — UFW firewall, fail2ban, SSL/TLS, secret management, access control
- **DevOps** — CI/CD with GitHub Actions, environment management, idempotent scripts
- **Infrastructure as Code** — repeatable, documented, version-controlled setup
- **Self-hosted cloud** — Nextcloud administration, external storage, user management

---

*Last updated: Q1 2026 | Maintainer: Data Engineering*
