# 🚀 Release Day Runbook — New Quarter Data

> Follow this checklist every quarter when TomTom releases new data.
> Total time with everything prepped: **~30 minutes of your time** (downloads run unattended).

---

## ⏰ Before Release Day (Do This Now)

These steps take 5 minutes and mean zero stress on release day.

### 1. Create the folder structure

```bash
cd /mnt/data/MapFlow/mapflow
bash /opt/nextcloud-setup/nextcloud-data-distribution/scripts/create_structure.sh \
    --quarter Q2_2026
```

✅ All destination folders exist and ready to receive data.

### 2. Prepare `.folders.env` for Q2

```bash
nano /opt/nextcloud-setup/nextcloud-data-distribution/.folders.env
```

Update:
```bash
DISTRO_FOLDER=/mnt/data/DISTRIBUTION_Q2_2026

# Update source folder names to match new download names
# (you'll know these once TomTom releases — pattern is always the same)
# MNR_MEA=mea_q2_202606003_mnr/data/mea
# MNR_EUR=eur_q2_202606003_mnr/data/eur
# etc.
```

Leave the source paths commented out for now — fill them in on release day.

### 3. Prepare the quarterly config

```bash
nano /opt/nextcloud-setup/nextcloud-data-distribution/quarterly/Q2_2026.yaml
```

Update `tomtom_version` once you know it (usually `2026.06.000` for MN, `2026.06.003` for MNR).

### 4. Dry-run the full pipeline

```bash
cd /opt/nextcloud-setup/nextcloud-data-distribution
python3 master_pipeline.py --quarter Q2_2026 --dry-run --skip-download
```

This validates the entire pipeline without touching anything. Fix any issues now.

---

## 📅 Release Day Checklist

### When TomTom notifies you data is available:

**Step 1 — Note the version number** (check TomTom portal or email)
```
MNR version: 2026.06.003  (usually .003 for MNR)
MN version:  2026.06.000  (usually .000 for MN)
```

**Step 2 — Update download.sh**
```bash
nano /mnt/data/MapFlow/mapflow/download.sh
# Change: TOMTOM_VERSION="2026.06.003"
# Change: QUARTER="Q2_2026"
# Uncomment the regions you need
```

**Step 3 — Start downloads** (runs unattended — can take hours)
```bash
cd /mnt/data/MapFlow/mapflow
source .venv/bin/activate

# Run once per product type — start MNR first (biggest)
bash download.sh   # edit FAMILY=MNR first

# Then edit FAMILY=MN and run again
bash download.sh

# Then SP, POI, APT as needed
```

**Step 4 — While downloading, update .folders.env**
```bash
nano /opt/nextcloud-setup/nextcloud-data-distribution/.folders.env
```

Set the source paths to match the download folder names:
```bash
DISTRO_FOLDER=/mnt/data/DISTRIBUTION_Q2_2026
MNR_MEA=MultiNet-R_MEA_2026.06.003_full_commercial/data/mea
MNR_EUR=MultiNet-R_EUR_2026.06.003_full_commercial/data/eur
MN_MEA=MultiNet_MEA_2026.06.000_full_commercial/data/mea
MN_EUR=MultiNet_EUR_2026.06.000_full_commercial/data/eur
SP_EUR=MultiNet-SpeedProfile_EUR_2026.06.000_full_commercial/data/eur
POI_MEA=MultiNet-POI_MEA_2026.06.000_full_commercial/data/mea
APT_MEA=MultiNet-AddressPoints_MEA_2026.06.000_full_commercial/data/mea
```

> **Tip:** Check actual folder names after download:
> ```bash
> ls /mnt/data/downloads/
> ```

**Step 5 — Run ingest** (after all downloads complete)
```bash
cd /opt/nextcloud-setup/nextcloud-data-distribution

# Always dry-run first
sudo bash scripts/ingest.sh --env .folders.env --dry-run

# If it looks correct, run for real
sudo bash scripts/ingest.sh \
    --env .folders.env \
    --log /mnt/data/MapFlow/logs/ingest_Q2_2026.log
```

**Step 6 — Verify data**
```bash
# Check folder sizes
du -sh /mnt/data/DISTRIBUTION_Q2_2026/*

# Check a specific client's data is there
ls /mnt/data/DISTRIBUTION_Q2_2026/MNR/MNR_MEA/zaf/
```

**Step 7 — Update Nextcloud**
```bash
cd /opt/nextcloud-setup/nextcloud-data-distribution

# Update CSV and provision Nextcloud
sudo python3 master_pipeline.py \
    --quarter Q2_2026 \
    --skip-download \
    --skip-ingest
```

**Step 8 — Notify clients**
```bash
sudo .venv/bin/python3 agents/agent_notify.py \
    --env config/.env \
    --quarter Q2_2026 \
    --dry-run   # preview first

# Then send for real
sudo .venv/bin/python3 agents/agent_notify.py \
    --env config/.env \
    --quarter Q2_2026
```

**Step 9 — Commit to GitHub**
```bash
cd /opt/nextcloud-setup/nextcloud-data-distribution
git add data/client_distribution.csv quarterly/Q2_2026.yaml
git commit -m "data: Q2 2026 distribution complete"
git push
```

---

## 🔍 Troubleshooting

### Download failed halfway through
Just re-run `bash download.sh` — Mapflow checks if files exist before downloading. It resumes automatically.

### Wrong folder names in .folders.env
```bash
# Check what Mapflow actually created
ls /mnt/data/downloads/ | grep -i "2026.06"
```

Copy the exact folder name into `.folders.env`.

### Nextcloud clients can't see new data
```bash
sudo -u www-data php /var/www/html/nextcloud/occ files:scan --all
```

### Check download progress
```bash
# Watch files appearing in real time
watch -n 5 'du -sh /mnt/data/downloads/*2026.06* 2>/dev/null'
```

### Check disk space before downloading
```bash
df -h /mnt/data
# Need at least 500GB free for a full quarter
```

---

## 📊 Typical Quarter Timeline

| Time | Action |
|---|---|
| Week before release | Run `create_structure.sh`, prep configs |
| Release day morning | Update version numbers, start MNR download |
| Release day afternoon | Start MN + products downloads |
| Next day | Run `ingest.sh`, verify data |
| Next day | Update Nextcloud, notify clients |

---

## 🗂️ Key File Locations

| File | Path | Purpose |
|---|---|---|
| Download script | `/mnt/data/MapFlow/mapflow/download.sh` | Downloads from TomTom |
| Folder creator | `scripts/create_structure.sh` | Creates Q folder structure |
| Ingest script | `scripts/ingest.sh` | Organises downloaded files |
| Quarterly config | `quarterly/Q2_2026.yaml` | Client→dataset mapping |
| Folders env | `.folders.env` | Source→dest path mapping |
| Master pipeline | `master_pipeline.py` | Runs everything |
| Client CSV | `data/client_distribution.csv` | Nextcloud provisioning |

---

*Last updated: Q1 2026 — update version numbers each quarter*
