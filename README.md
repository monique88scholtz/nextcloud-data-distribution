# 🗺️ GeoInt Data Distribution Platform

> Automated geospatial data distribution system — from TomTom API download to client delivery via self-hosted Nextcloud.
> Built by a junior data engineer, migrating from OwnCloud, designed for zero-touch quarterly releases.

[![Validate Pipeline](https://github.com/monique88scholtz/nextcloud-data-distribution/actions/workflows/validate.yml/badge.svg)](https://github.com/monique88scholtz/nextcloud-data-distribution/actions)

---

## 🗺️ What This Does

Each quarter, TomTom releases new geospatial map data. This system automates the entire distribution workflow:

```
TomTom API                     Provider SFTP
     │                               │
     │  download.sh                  │  download_mapit.sh
     ▼                               ▼
/mnt/data/downloads/          /mnt/data/downloads/mapit/
          │                               │
          └──────────────┬────────────────┘
                         │
                         │  ingest.sh
                         ▼
          /mnt/data/DISTRIBUTION_Q1_2026/
              MNR/  MN/  PRODUCTS/  MAPIT/
                         │
                         │  provision_clients.py
                         ▼
                    Nextcloud
              (clients see only their data)
                         │
                         │  agent_notify.py
                         ▼
              Email notification to each client
```

**One command runs the entire pipeline:**
```bash
python3 master_pipeline.py --quarter Q2_2026
```

---

## 🏗️ Project Structure

```
geoint-distribution/
├── install_nextcloud.sh          # Full Nextcloud server installer
├── master_pipeline.py            # ONE command — full quarterly pipeline
├── agents/
│   ├── agent_provision.py        # Auto-provisions users/groups/mounts from CSV
│   ├── agent_refresh.py          # Keeps Nextcloud file cache fresh (runs every 60s)
│   └── agent_notify.py           # Emails clients when new data is available
├── scripts/
│   ├── download.sh               # Downloads TomTom data via Mapflow
│   ├── download_mapit.sh         # Downloads MAPIT data from provider SFTP
│   ├── create_structure.sh       # Creates quarterly folder structure
│   ├── ingest.sh                 # Organises downloaded files into distribution structure
│   ├── provision_clients.py      # Creates Nextcloud users, groups, mounts from CSV
│   ├── update_quarter.py         # Diffs old vs new CSV, applies changes
│   ├── validate_csv.py           # Validates CSV before provisioning
│   ├── healthcheck.py            # One-command system health check
│   ├── backup.sh                 # Automated daily backup
│   └── folders.env.template      # Template for ingest configuration
├── agents/
├── quarterly/
│   └── Q2_2026.yaml              # Master config: clients → datasets → versions
├── config/
│   ├── .env.template             # Safe template (never commit .env)
│   └── nextcloud-refresh.service # Systemd service for refresh agent
├── data/
│   └── client_distribution.csv  # Client access mapping (current quarter)
└── docs/
    ├── runbook.md                # Day-to-day operations guide
    ├── release_day_runbook.md    # Step-by-step quarterly release checklist
    ├── ingestion_runbook.md      # Data ingestion pipeline guide
    └── architecture.md           # System design and decisions
```

---

## 🚀 Quick Start

### New Server Setup

```bash
# 1. Clone the repo
git clone https://github.com/monique88scholtz/nextcloud-data-distribution.git
cd nextcloud-data-distribution

# 2. Configure
cp config/.env.template config/.env
nano config/.env          # fill in passwords, domain, etc.
chmod 600 config/.env

# 3. Install Nextcloud + provision all clients
sudo bash install_nextcloud.sh
```

### Every Quarter

```bash
# 1. Create folder structure for new quarter (do this early)
sudo bash scripts/create_structure.sh --quarter Q2_2026

# 2. When TomTom data drops — download it
cd /mnt/data/MapFlow/mapflow && source .venv/bin/activate
bash scripts/download.sh       # TomTom data
bash scripts/download_mapit.sh # MAPIT data

# 3. Organise + provision + notify — one command
python3 master_pipeline.py --quarter Q2_2026
```

---

## 📋 The Quarterly Config

Everything about a quarter lives in one YAML file (`quarterly/Q2_2026.yaml`):

```yaml
quarter: Q2_2026
tomtom_version: "2026.06.003"
distro_folder: /mnt/data/DISTRIBUTION_Q2_2026

datasets:
  MNR_MEA:
    family: MNR
    region: MEA
    zones: ~ALL
    mnr_data: [ALL]

clients:
  Esri:
    username: esri
    datasets: [MNR_MEA, MNR_EUR, MN_MEA, POI_MEA, SP_MEA]
  Altrack:
    username: altrack
    datasets: [MNR_SOUTHERN_AFRICA, POI_MEA]
```

Change the version, add/remove clients or datasets — everything downstream updates automatically.

---

## 🔒 Security Design

- Each client has their own Nextcloud account
- Clients are scoped to groups — they only see their group's mounts
- Username autocomplete disabled — clients can't discover each other
- Passwords enforced on share links, with expiry dates
- Resharing disabled
- Firewall: only ports 22, 80, 443 open
- SSL via Let's Encrypt (once domain is configured)
- Secrets in `.env` only — never committed to git

---

## 🤖 Agents

### Refresh Agent (runs every 60 seconds)
Keeps Nextcloud file listings current so clients always see the latest data.

```bash
sudo systemctl enable nextcloud-refresh
sudo systemctl start nextcloud-refresh
```

### Provision Agent (watches for CSV changes)
Drop a new CSV — it auto-provisions users, groups, and mounts.

```bash
sudo python3 agents/agent_provision.py --watch
```

### Notify Agent
Sends each client a personalised email with their credentials and dataset list.

```bash
sudo python3 agents/agent_notify.py --quarter Q2_2026 --dry-run
```

---

## 🛠️ Tech Stack

| Layer | Technology |
|---|---|
| OS | Ubuntu 22.04 LTS |
| Web server | Apache 2 |
| Database | MariaDB |
| Cache | Redis |
| Cloud platform | Nextcloud (self-hosted) |
| Download automation | Mapflow (Python) + TomTom API |
| Provisioning | Python 3 (pandas, subprocess) |
| CI/CD | GitHub Actions |
| SSL | Let's Encrypt / Certbot |
| Secrets | `.env` file, `chmod 600` |

---

## 💼 Skills Demonstrated

This project covers the full data engineering stack:

- **Linux server administration** — Ubuntu, Apache, MariaDB, Redis, systemd services
- **Python automation** — pandas, subprocess, YAML config, CSV ingestion, agents
- **REST API integration** — TomTom Data Catalogue API (pagination, auth, download)
- **SFTP automation** — paramiko-based file transfer from provider servers
- **Data pipeline design** — validate → download → ingest → provision → notify
- **Security** — UFW firewall, SSL, secret management, access control
- **DevOps** — GitHub Actions CI, idempotent scripts, environment management
- **Self-hosted cloud** — Nextcloud administration, external storage, user/group management
- **Documentation** — runbooks, architecture docs, release checklists

---

## 📚 Documentation

- [Operations Runbook](docs/runbook.md) — day-to-day commands
- [Release Day Runbook](docs/release_day_runbook.md) — quarterly release checklist
- [Ingestion Runbook](docs/ingestion_runbook.md) — data pipeline guide
- [Architecture](docs/architecture.md) — system design decisions

---

## ⚠️ Note on Company Data

This repository contains no proprietary data, client information, or credentials.
The `data/client_distribution.csv` file uses anonymised/sample data.
All sensitive configuration is stored in `config/.env` which is excluded from git via `.gitignore`.

---

*Built during Q1 2026 — migrating from OwnCloud to self-hosted Nextcloud with full pipeline automation.*
*Junior data engineer. No mentorship. Figure it out. Ship it.*
