# 📦 Nextcloud Data Distribution System

> Automated Nextcloud setup + quarterly client data provisioning  
> Built as a real-world data engineering project migrating from OwnCloud.

---

## 🗺️ What This Does

This project automates the full lifecycle of a **secure data distribution platform**:

1. **Installs Nextcloud** on Ubuntu (Apache, MariaDB, PHP, Redis, SSL)
2. **Reads a quarterly CSV** (`CLIENT / PASSWORD / DATA / GROUP / DirectoryPath`)
3. **Provisions each client** automatically — user account, groups, and folder access
4. **Enforces strict isolation** — clients can only see exactly what's shared with them
5. **Supports quarterly updates** — add/remove clients and access between quarters

---

## 🏗️ Project Structure

```
nextcloud-data-distribution/
├── install_nextcloud.sh          # Full server install + provisioning (run once)
├── config/
│   ├── .env.template             # Safe template — copy to .env and fill in
│   └── .env                      # ← NOT committed to git (your real secrets)
├── data/
│   └── client_distribution.csv  # Quarterly distribution CSV
├── scripts/
│   ├── provision_clients.py      # Creates users, groups, mounts from CSV
│   ├── update_quarter.py         # Diffs old vs new CSV, applies changes
│   └── validate_csv.py           # Validates CSV before provisioning
├── docs/
│   └── architecture.md           # System design notes
└── .github/
    └── workflows/
        └── validate.yml          # CI: validates CSV + dry-run on every push
```

---

## 🚀 Quick Start

### 1. Clone & configure

```bash
git clone https://github.com/YOUR_USERNAME/nextcloud-data-distribution.git
cd nextcloud-data-distribution

# Copy the template and fill in your values
cp config/.env.template config/.env
nano config/.env
```

### 2. Add your CSV

```bash
cp /path/to/your/Quarterly_Data_Dist.csv data/client_distribution.csv
```

### 3. Validate the CSV first

```bash
python3 scripts/validate_csv.py --csv data/client_distribution.csv
```

### 4. Run the installer (on your Ubuntu server)

```bash
chmod +x install_nextcloud.sh
sudo ./install_nextcloud.sh
```

This single command installs everything AND provisions all clients from the CSV.

---

## 🔄 Each New Quarter

When you get a new distribution CSV:

```bash
# 1. Save the old CSV as reference
cp data/client_distribution.csv data/prev_quarter.csv

# 2. Copy in the new CSV
cp /path/to/new/Q2_Distribution.csv data/client_distribution.csv

# 3. Validate it
python3 scripts/validate_csv.py --csv data/client_distribution.csv

# 4. Preview changes (dry-run)
sudo python3 scripts/update_quarter.py \
    --env config/.env \
    --old data/prev_quarter.csv \
    --new data/client_distribution.csv \
    --dry-run

# 5. Apply changes
sudo python3 scripts/update_quarter.py \
    --env config/.env \
    --old data/prev_quarter.csv \
    --new data/client_distribution.csv
```

---

## 🔒 Security Design

| Feature | How it's enforced |
|---|---|
| Client isolation | Each client = one user, access scoped to their group only |
| No cross-client visibility | Username autocomplete disabled |
| Passwords on share links | Enforced via occ config |
| Link expiry | 30 days by default, enforced |
| No resharing | Recipients cannot forward access |
| HTTPS | Let's Encrypt via Certbot |
| Secrets management | `.env` file, never committed to git |

---

## 📋 CSV Format

| Column | Description |
|---|---|
| `CLIENT` | Client name (only on first row per client — rest are blank) |
| `PASSWORD` | UUID password (only on first row per client) |
| `DATA` | Human-readable dataset name |
| `GROUP` | Nextcloud group ID this dataset belongs to |
| `DirectoryPath` | Absolute path on the server to mount |

---

## 🛠️ Tech Stack

- **OS**: Ubuntu 20.04 / 22.04 / 24.04
- **Web**: Apache 2 + mod_rewrite
- **PHP**: 8.x with OPcache + APCu
- **Database**: MariaDB
- **Cache**: Redis
- **Platform**: Nextcloud (self-hosted)
- **Automation**: Python 3 (pandas, python-dotenv)
- **CI/CD**: GitHub Actions
- **SSL**: Let's Encrypt / Certbot

---

## 💡 Skills Demonstrated

- Linux server administration
- Automated provisioning from structured data (CSV → API)
- Security-first access control design
- Idempotent infrastructure scripts (safe to re-run)
- CI validation pipelines (GitHub Actions)
- Environment-based secrets management
- Python data engineering (pandas, subprocess, dotenv)
- Quarterly delta-update pattern (diff old vs new)

---

## 📝 Setup on GitHub

```bash
# First time
git init
git remote add origin https://github.com/YOUR_USERNAME/nextcloud-data-distribution.git
git add .
git commit -m "feat: initial Nextcloud distribution setup"
git push -u origin main

# Each quarter
git add data/client_distribution.csv
git commit -m "data: Q2 2026 distribution update"
git push
```

> ⚠️ The `.gitignore` ensures `config/.env` is **never** pushed to GitHub.

---

## 🧑‍💻 Author

Built by a junior data engineer learning infrastructure automation in production.  
Migrated from OwnCloud to self-hosted Nextcloud with full automation.
