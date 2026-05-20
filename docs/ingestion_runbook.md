# 📥 Data Ingestion Runbook

> How to ingest a new quarter of data from downloads into the distribution folder structure.

---

## Overview

```
/mnt/data/downloads/          ← raw downloaded data (source)
        │
        │  bash ingest.sh
        ▼
/mnt/data/DISTRIBUTION_Q1_2026/   ← organised distribution structure (dest)
    ├── MNR/
    │   ├── MNR_MEA/              country subfolders e.g. /zaf/zaf/core/
    │   ├── MNR_EUR/
    │   └── MNR_DOCUMENTATION/
    ├── MN/
    │   ├── MN_MEA/
    │   ├── MN_EUR/
    │   └── MN_DOCUMENTATION/
    └── PRODUCTS/
        ├── MN_PREMIUM_POI/
        ├── SPEED_PROFILES/
        └── MN_APT/
```

---

## Every Quarter — Step by Step

### Step 1 — Update `.folders.env`

```bash
nano .folders.env
```

Change:
- `DISTRO_FOLDER` → new quarter path e.g. `/mnt/data/DISTRIBUTION_Q2_2026`
- Update source folder names to match new download folder names

Example — Q1 → Q2:
```bash
# Before
DISTRO_FOLDER=/mnt/data/DISTRIBUTION_Q1_2026
MNR_MEA=mea_q1_202603003_mnr/data/mea

# After
DISTRO_FOLDER=/mnt/data/DISTRIBUTION_Q2_2026
MNR_MEA=mea_q2_202606003_mnr/data/mea
```

### Step 2 — Check what's in downloads

```bash
ls /mnt/data/downloads/
```

Make sure each folder listed in `.folders.env` actually exists.

### Step 3 — Dry run first (ALWAYS)

```bash
sudo bash ingest.sh --env .folders.env --dry-run
```

Read the output carefully:
- `Source OK` → path exists, will be processed
- `Source MISSING` → path doesn't exist, will be skipped
- `DRYRUN:` → shows exactly what would happen
- No files are touched in dry-run mode

### Step 4 — Run ingestion

```bash
sudo bash ingest.sh \
    --env .folders.env \
    --log /mnt/data/logs/ingest_q2_2026.log
```

This will:
1. Copy files from downloads → distribution folder
2. Tidy any MN files that landed at root level into country subfolders
3. Remove duplicate source files (same name + same size)
4. Trigger Nextcloud file scan so clients see new data immediately

### Step 5 — Verify

```bash
# Check the log
tail -50 /mnt/data/logs/ingest_q2_2026.log

# Check destination structure
ls /mnt/data/DISTRIBUTION_Q2_2026/

# Check disk usage
du -sh /mnt/data/DISTRIBUTION_Q2_2026/*

# Trigger a manual Nextcloud scan if needed
sudo -u www-data php /var/www/html/nextcloud/occ files:scan --all
```

---

## Common Options

```bash
# Only ingest specific datasets
sudo bash ingest.sh --env .folders.env --only MNR_MEA,MNR_EUR

# Skip specific datasets
sudo bash ingest.sh --env .folders.env --skip SP_MEA,APT_MEA

# Move mode — deletes source files after verified copy
# Use this when you want to free up space in /mnt/data/downloads
sudo bash ingest.sh --env .folders.env --move

# Skip the Nextcloud scan (useful if running multiple times)
sudo bash ingest.sh --env .folders.env --skip-nextcloud

# Full run with log
sudo bash ingest.sh \
    --env .folders.env \
    --log /mnt/data/logs/ingest_$(date +%Y%m%d).log
```

---

## What ingest.sh Does — Step by Step

| Step | What happens |
|---|---|
| Pre-flight | Checks commands exist, disk space, source paths |
| Structure | Creates all destination folders |
| Copy | Copies files with atomic write (temp → rename) |
| Size check | Skips if dest exists at same size, replaces if src is bigger |
| Tidy | Moves any MN root-level files into country subfolders |
| Cleanup | Removes source files already confirmed in destination |
| Nextcloud | Triggers `occ files:scan --all` |
| Summary | Prints counts of copied/skipped/replaced/errors + time taken |

---

## Safe to Re-Run

The script is **idempotent** — safe to run multiple times:
- Files already at destination with the same size are skipped
- Only missing or smaller destination files get copied
- No data is lost

---

## Troubleshooting

### "Source MISSING" warning
```
⚠️ Source MISSING: MNR_MEA → /mnt/data/downloads/mea_q1_...
```
The folder in `.folders.env` doesn't exist. Check:
```bash
ls /mnt/data/downloads/ | grep mea
```
Update the path in `.folders.env` to match the actual folder name.

### Files copied but clients can't see them in Nextcloud
```bash
sudo -u www-data php /var/www/html/nextcloud/occ files:scan --all
```

### Size mismatch warning after copy
```
⚠️ Size mismatch after copy! — keeping dest, NOT removing src
```
The copy may have been interrupted. The source file is kept safe.
Run again — it will detect the dest is smaller and re-copy.

### Script stopped halfway through
Just re-run it. Files already copied at the correct size will be skipped.
It will pick up from where it left off.

---

## Folder Structure Explained

### MNR (MultiNet-R)
```
MNR_MEA/
  └── zaf/          ← country code (ISO 3166-1 alpha-3)
      └── zaf/
          ├── core/
          ├── apt/
          ├── buildings/
          └── ...
```

### MN (MultiNet)
```
MN_MEA/
  └── zaf/          ← country code
      └── zaf.tar.gz etc.
```

MN files are identified by `-mn-<cc>-` in the filename.
The `tidy` step automatically moves root-level MN files into the correct country subfolder.

### Products (POI, Speed Profiles, APT)
```
MN_PREMIUM_POI_MEA/
  └── zaf/          ← country code
      └── poi files

SPEED_PROFILES_MEA/
  └── zaf/
      └── speed profile files
```

---

## Adding a New Dataset Type

1. Add a variable to `.folders.env`:
   ```bash
   MNR_SOUTHERN_AFRICA=southern_africa_q2_2026_mnr/data/southern_africa
   ```

2. `ingest.sh` will automatically pick it up — no code changes needed.
   The key prefix determines where it goes:
   - `MNR_*` → `DISTRO_FOLDER/MNR/MNR_{REGION}/`
   - `MN_*` → `DISTRO_FOLDER/MN/MN_{REGION}/`
   - `SP_*` → `DISTRO_FOLDER/PRODUCTS/SPEED_PROFILES/SPEED_PROFILES_{REGION}/`
   - `POI_*` → `DISTRO_FOLDER/PRODUCTS/MN_PREMIUM_POI/MN_PREMIUM_POI_{REGION}/`
   - `APT_*` → `DISTRO_FOLDER/PRODUCTS/MN_APT/MN_APT_{REGION}/`

---

*Last updated: Q1 2026*
