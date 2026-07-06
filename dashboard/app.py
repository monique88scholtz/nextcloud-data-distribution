#!/usr/bin/env python3
"""
dashboard/app.py — GeoInt Distribution Dashboard v5
Smart pipeline board: auto-detects downloads, notifications, guided workflow
"""

import json
import os
import time
import re
import smtplib
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import yaml
from dotenv import dotenv_values
from flask import Flask, jsonify, make_response, render_template, request

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True
app.jinja_env.cache = {}

BASE_DIR    = Path("/opt/nextcloud-setup/nextcloud-data-distribution")
SCRIPTS     = BASE_DIR / "scripts"
AGENTS      = BASE_DIR / "agents"
QUARTERLY   = BASE_DIR / "quarterly"
DATA_DIR    = BASE_DIR / "data"
CONFIG_ENV  = BASE_DIR / "config" / ".env"
FOLDERS_ENV = BASE_DIR / ".folders.env"
MAPFLOW     = Path("/mnt/data/MapFlow/mapflow")
LOGS_DIR    = Path("/mnt/data/MapFlow/logs")
DOWNLOADS     = Path("/mnt/data/downloads/downloads/downloads")  # Mapflow triple-nested
DOWNLOADS_RAW = Path("/mnt/data/downloads/downloads/downloads")
DISTRO_BASE = Path("/mnt/data")
NC_OCC      = Path("/var/www/html/nextcloud/occ")
SUPPORT_EMAIL = "support@geoint.africa"

LOGS_DIR.mkdir(parents=True, exist_ok=True)
running_jobs   = {}
notified_jobs  = set()   # track which completed jobs we've emailed about


# ── Helpers ───────────────────────────────────────────────────
def load_env():
    try:
        return dotenv_values(str(CONFIG_ENV))
    except:
        return {}


def nc_url():
    env = load_env()
    domain = env.get("NC_DOMAIN", "")
    ssl    = env.get("ENABLE_SSL", "false").lower() == "true"
    if domain:
        return f"{'https' if ssl else 'http'}://{domain}/"
    return "http://localhost/"


def send_notification_email(subject, body_html, body_text=""):
    """Send email notification to support@geoint.africa"""
    try:
        env = load_env()
        smtp_host = env.get("SMTP_HOST", "")
        smtp_port = int(env.get("SMTP_PORT", 587))
        smtp_user = env.get("SMTP_USER", "")
        smtp_pass = env.get("SMTP_PASSWORD", "")

        if not smtp_host or not smtp_user:
            return False, "SMTP not configured in .env"

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = env.get("SMTP_FROM", smtp_user)
        msg["To"]      = SUPPORT_EMAIL

        if body_text:
            msg.attach(MIMEText(body_text, "plain"))
        msg.attach(MIMEText(body_html, "html"))

        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        return True, "sent"
    except Exception as e:
        return False, str(e)


# ── Quarter & dataset detection ───────────────────────────────
# Maps family to the exact download folder name pattern
# Key: family string as used in the YAML
# Value: lambda(region, version) -> folder name
FOLDER_PATTERNS = {
    "MNR":   lambda r, v: f"MultiNet-R_{r}_{v}_full_commercial",
    "MN":    lambda r, v: f"MultiNet_{r}_{v}_full_commercial",
    "MNSP":  lambda r, v: f"MultiNet-SpeedProfile_{r}_{v}_full_commercial",
    "MNPOI": lambda r, v: f"MultiNet-POI_{r}_{v}_full_commercial",
    "MNAP":  lambda r, v: f"MultiNet-AddressPoints_{r}_{v}_full_commercial",
    "MAPIT": lambda r, v: f"mapit_{r}",  # MAPIT has no TomTom download folder
}

DATA_SUFFIX = {
    "MNR":   lambda r: f"data/{r.lower()}",
    "MN":    lambda r: f"data/{r.lower()}",
    "MNSP":  lambda r: f"data/{r.lower()}",
    "MNPOI": lambda r: f"data/{r.lower()}",
    "MNAP":  lambda r: f"data/{r.lower()}",
    "MAPIT": lambda r: r,
}

# Datasets that are subsets of a larger regional download.
# e.g. MNR_ZAF and MNR_SOUTHERN_AFRICA both come from the MNR_MEA download.
# These share the same source folder — their download status depends on
# whether the parent regional download exists.
SUBSET_SOURCE = {
    "MNR_SOUTHERN_AFRICA": ("MNR", "MEA"),
    "MNR_ZAF":             ("MNR", "MEA"),
    "MNR_ZAF_PLUS":        ("MNR", "MEA"),
    "POI_SOUTHERN_AFRICA": ("MNPOI", "MEA"),
    "POI_ZAF":             ("MNPOI", "MEA"),
}


def get_prefix(key):
    return key.split("_")[0]


CLIENTS_META_DIR = Path("/mnt/data/downloads/downloads/clients/GEOINT")


def find_latest_layers_manifest(folder_pattern):
    """
    Find the most recently modified *_layers.json file matching the given
    download folder name (e.g. "MultiNet_SEA_2026.06.000_full_commercial").
    MapFlow writes one of these per download attempt under a run-specific
    UUID folder, so the most recently modified one is the correct manifest
    to verify against - it reflects what TomTom said should exist for the
    most recent attempt at this exact version.
    """
    if not CLIENTS_META_DIR.exists():
        return None
    candidates = list(CLIENTS_META_DIR.glob(f"*/{folder_pattern}_layers.json"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def verify_download_against_manifest(download_folder: Path, folder_name: str):
    """
    Compare what TomTom's API says should exist (from the layers.json
    manifest MapFlow saves per download attempt) against what is actually
    on disk right now, checking both file presence and exact file size.

    Returns a dict with a plain-language summary suitable for display to
    a non-technical person, plus the underlying numbers for the UI to
    render a progress indicator.
    """
    manifest_path = find_latest_layers_manifest(folder_name)
    if not manifest_path:
        return {
            "verified": False,
            "reason": "No TomTom manifest found to verify against",
        }

    try:
        with open(manifest_path) as f:
            zones_data = json.load(f)
    except Exception as e:
        return {"verified": False, "reason": f"Could not read manifest: {e}"}

    expected_total = 0
    expected_size = 0
    missing_files = []
    missing_size = 0

    for zone_dict in zones_data:
        for zone, files in zone_dict.items():
            for finfo in files:
                expected_total += 1
                expected_size += finfo.get("filesize", 0)
                fname = finfo.get("filename", "")
                fpath_rel = finfo.get("filepath", "/data").strip("/")
                on_disk = download_folder / fpath_rel / fname
                ok = False
                if on_disk.exists():
                    try:
                        actual_size = on_disk.stat().st_size
                        expected_file_size = finfo.get("filesize", 0)
                        # Allow exact match only - a partial file is not complete
                        ok = (actual_size == expected_file_size and actual_size > 0)
                    except Exception:
                        ok = False
                if not ok:
                    missing_files.append(fname)
                    missing_size += finfo.get("filesize", 0)

    present_count = expected_total - len(missing_files)
    percent = round((present_count / expected_total) * 100, 1) if expected_total else 0
    complete = (len(missing_files) == 0)

    return {
        "verified": True,
        "complete": complete,
        "percent_complete": percent,
        "expected_files": expected_total,
        "present_files": present_count,
        "missing_files_count": len(missing_files),
        "missing_size_gb": round(missing_size / (1024**3), 2),
        "expected_size_gb": round(expected_size / (1024**3), 2),
        "missing_files_sample": missing_files[:10],
        "manifest_used": str(manifest_path),
    }


def check_dataset_incomplete(full_path):
    """
    Scan a download folder for signs of an interrupted download:
    - .aria2 control files (aria2's own marker for unfinished downloads)
    - .aria2__temp lock files (left behind when aria2c is killed mid-write)
    - 0-byte data files (placeholder created but never written to)
    Returns the count of incomplete file markers found.
    """
    try:
        aria2_markers = sum(1 for _ in full_path.rglob("*.aria2*"))
        zero_byte = sum(
            1 for f in full_path.rglob("*")
            if f.is_file()
            and not f.name.endswith((".aria2", ".aria2__temp", ".meta4"))
            and f.stat().st_size == 0
        )
        return aria2_markers + zero_byte
    except Exception:
        return 0


def check_dataset_downloaded(key, family, region, version):
    """
    Check if a dataset's source folder exists on disk with actual data.
    Correctly distinguishes between different product families.
    Subset datasets (e.g. MNR_ZAF) check their parent regional download.

    Returns: (downloaded, folder_name, size, incomplete_count)
    incomplete_count > 0 means aria2 control files or 0-byte placeholders
    were found - the download exists but is not actually finished.
    """
    if key in SUBSET_SOURCE:
        source_family, source_region = SUBSET_SOURCE[key]
    else:
        source_family  = family
        source_region  = region

    pattern_fn = FOLDER_PATTERNS.get(source_family)
    if not pattern_fn:
        return False, None, None, 0

    if source_family == "MAPIT":
        return False, "mapit (manual SFTP download)", None, 0
    folder_name = pattern_fn(source_region, version)
    full_path   = DOWNLOADS / folder_name
    if full_path.exists():
        try:
            has_data = any(True for _ in full_path.rglob("*.tar.gz")) or any(True for _ in full_path.rglob("*.7z.001"))
            if not has_data:
                has_data = sum(1 for _ in full_path.rglob("*") if _.is_file()) > 5
            if has_data:
                incomplete_count = check_dataset_incomplete(full_path)
                try:
                    r = subprocess.run(["du", "-sh", str(full_path)],
                                       capture_output=True, text=True, timeout=10)
                    size = r.stdout.split()[0] if r.stdout else "?"
                except:
                    size = "?"
                return True, folder_name, size, incomplete_count
        except:
            pass
    return False, folder_name, None, 0


def get_pipeline_status(quarter):
    """
    Return full pipeline status for a quarter.
    Auto-detects what's downloaded, ingested, provisioned.
    """
    config_path = QUARTERLY / f"{quarter}.yaml"
    if not config_path.exists():
        return None

    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    version       = cfg.get("tomtom_version", "")
    distro        = cfg.get("distro_folder", "")
    datasets      = cfg.get("datasets", {})
    clients       = cfg.get("clients", {})

    distro_path   = Path(distro) if distro else None
    distro_exists = distro_path.exists() if distro_path else False

    dataset_status = {}
    total = len(datasets)
    downloaded_count = 0
    active_download  = None

    # Check ONCE if mapflow is running — outside the loop
    mapflow_running = False
    active_log_family = None
    active_log_quarter = None
    try:
        r = subprocess.run(["pgrep", "-f", "mapflow.main"],
                           capture_output=True, text=True)
        mapflow_running = bool(r.stdout.strip())
    except:
        pass

    # If running, check which product is actively downloading
    # by looking at the most recently modified log file
    # Track the exact dataset key(s) currently downloading (e.g. "MN_NAM"),
    # not just the family - otherwise every undownloaded MN_* dataset gets
    # falsely marked as "Downloading..." whenever ANY MN download is running.
    active_log_families = set()   # kept for any other code that reads this name
    active_dataset_keys = set()
    if mapflow_running:
        try:
            import time
            now = time.time()
            # Find logs modified in the last 10 minutes — these are active downloads
            for log in LOGS_DIR.glob("download_*.log"):
                if now - log.stat().st_mtime < 600:
                    # Log format: download_MN_EUR_Q2_2026.log or download_MNPOI_MEA_Q2_2026.log
                    stem = log.stem  # e.g. download_MN_EUR_Q2_2026
                    parts = stem.split("_")  # ['download', 'MN', 'EUR', 'Q2', '2026']
                    if len(parts) >= 2:
                        # Everything between 'download' and the quarter (Q#_####)
                        # is the actual dataset key, e.g. ['MN', 'EUR'] -> "MN_EUR"
                        key_parts = []
                        for p in parts[1:]:
                            if p.startswith("Q") and len(p) <= 3:
                                break
                            key_parts.append(p)
                        if key_parts:
                            full = key_parts[0]
                            active_log_families.add(full)
                            if len(key_parts) > 1:
                                active_log_families.add(key_parts[0])
                                active_dataset_keys.add("_".join(key_parts))
                            else:
                                active_dataset_keys.add(key_parts[0])
            active_log_family = next(iter(active_log_families), None)
        except:
            pass

    for key, ds in datasets.items():
        family = ds.get("family", get_prefix(key))
        region = ds.get("region", key.replace(f"{get_prefix(key)}_", ""))

        # Use correct version suffix per family — MNR=.003, all others=.000
        _ver = version if family == "MNR" else version.rsplit(".", 1)[0] + ".000"
        downloaded, folder, size, incomplete_count = check_dataset_downloaded(key, family, region, _ver)
        # Only mark as actively downloading if:
        # 1. Mapflow is running AND
        # 2. This dataset's family matches the active download AND
        # 3. This dataset is not already downloaded
        is_active = (
            mapflow_running
            and not downloaded
            and key in active_dataset_keys
        )

        # Check if ingested — requires actual .tar.gz data files in destination
        # Empty folders or folders with only subdirectories do NOT count as ingested
        ingested = False
        if distro_exists and distro_path:
            prefix = get_prefix(key)
            # Destination region = part of key after first underscore
            # e.g. MNR_SOUTHERN_AFRICA → SOUTHERN_AFRICA, MNR_MEA → MEA
            dest_region = key.split("_", 1)[1] if "_" in key else region
            dest_map = {
                "MNR":   distro_path / "MNR" / f"MNR_{dest_region}",
                "MN":    distro_path / "MN" / f"MN_{dest_region}",
                "SP":    distro_path / "PRODUCTS" / "SPEED_PROFILES" / f"SPEED_PROFILES_{dest_region}",
                "POI":   distro_path / "PRODUCTS" / "MN_PREMIUM_POI" / f"MN_PREMIUM_POI_{dest_region}",
                "APT":   distro_path / "PRODUCTS" / "MN_APT" / f"MN_APT_{dest_region}",
                "MAPIT": distro_path / "MAPIT" / "SQL",
            }
            dest = dest_map.get(prefix)
            if dest and dest.exists():
                try:
                    # Must have actual data files — tar.gz (MNR) or 7z.001 (MN)
                    file_count = sum(1 for _ in dest.rglob("*.tar.gz"))
                    if file_count == 0:
                        file_count = sum(1 for _ in dest.rglob("*.7z.001"))
                    ingested = file_count > 0
                except:
                    pass
        # Skip manifest verify if dataset is already ingested (--move mode
        # removes source files, leaving an empty/near-empty download folder
        # that would falsely show as 0% complete against the manifest)
        _dl_has_data = False
        if downloaded and folder:
            try:
                _p = DOWNLOADS / folder
                _dl_has_data = (
                    sum(1 for _ in _p.rglob('*.7z.001')) +
                    sum(1 for _ in _p.rglob('*.tar.gz'))
                ) > 10
            except Exception:
                pass
        verify_percent = None
        verify_missing_gb = None
        # Only verify if download folder has real data AND distribution dest doesn't
        # (if already ingested, dist folder is authoritative — skip download check)
        _dist_has_data = False
        if distro_exists and distro_path:
            try:
                _pref = get_prefix(key)
                _dreg = key.split('_', 1)[1] if '_' in key else region
                _dmap = {
                    'MNR': distro_path / 'MNR' / f'MNR_{_dreg}',
                    'MN':  distro_path / 'MN' / f'MN_{_dreg}',
                    'SP':  distro_path / 'PRODUCTS' / 'SPEED_PROFILES' / f'SPEED_PROFILES_{_dreg}',
                    'POI': distro_path / 'PRODUCTS' / 'MN_PREMIUM_POI' / f'MN_PREMIUM_POI_{_dreg}',
                    'APT': distro_path / 'PRODUCTS' / 'MN_APT' / f'MN_APT_{_dreg}',
                }
                _ddest = _dmap.get(_pref)
                if _ddest and _ddest.exists():
                    _dist_has_data = (
                        sum(1 for _ in _ddest.rglob('*.tar.gz')) +
                        sum(1 for _ in _ddest.rglob('*.7z.001'))
                    ) > 0
            except Exception:
                pass
        if _dl_has_data and not _dist_has_data:
            try:
                full_dl_path = DOWNLOADS / folder
                v = verify_download_against_manifest(full_dl_path, folder)
                if v.get('verified'):
                    verify_percent = v['percent_complete']
                    verify_missing_gb = v['missing_size_gb']
                    if not v['complete']:
                        incomplete_count = max(incomplete_count, v['missing_files_count'])
            except Exception:
                pass
        # Subset datasets share a source with a parent dataset.
        # They don't require a separate download, but they ARE
        # available to ingest once the parent is downloaded.
        is_subset = key in SUBSET_SOURCE
        subset_of = (SUBSET_SOURCE[key][0] + "_" + SUBSET_SOURCE[key][1]) if is_subset else None

        if downloaded and not is_subset:
            downloaded_count += 1

        # Set active_download to the key of the dataset currently downloading
        if is_active:
            active_download = key

        # Size: show destination size if ingested, source size if just downloaded
        display_size = size  # source size by default
        if ingested and distro_path:
            prefix = get_prefix(key)
            dest_region = key.split("_", 1)[1] if "_" in key else region
            dest_map = {
                "MNR": distro_path / "MNR" / f"MNR_{dest_region}",
                "MN":  distro_path / "MN" / f"MN_{dest_region}",
                "SP":  distro_path / "PRODUCTS" / "SPEED_PROFILES" / f"SPEED_PROFILES_{dest_region}",
                "POI": distro_path / "PRODUCTS" / "MN_PREMIUM_POI" / f"MN_PREMIUM_POI_{dest_region}",
                "APT": distro_path / "PRODUCTS" / "MN_APT" / f"MN_APT_{dest_region}",
            }
            dest = dest_map.get(prefix)
            if dest and dest.exists():
                try:
                    r = subprocess.run(
                        ["du", "-sh", str(dest)],
                        capture_output=True, text=True, timeout=30)
                    if r.stdout:
                        display_size = r.stdout.split()[0]
                except:
                    pass

        # If already ingested, it was definitely downloaded
        if ingested:
            downloaded = True
        dataset_status[key] = {
            "key":        key,
            "family":     family,
            "region":     region,
            "downloaded": downloaded,
            "is_subset":  is_subset,
            "subset_of":  subset_of,
            "folder":     folder,
            "size":       display_size,
            "ingested":   ingested,
            "active":     is_active,
            "incomplete_count": incomplete_count,
            "verify_percent": verify_percent,
            "verify_missing_gb": verify_missing_gb,
        }

    # Check if Nextcloud is provisioned for THIS quarter.
    # We only consider provisioning done if:
    # 1. The distribution folder for this quarter exists, AND
    # 2. Client accounts exist in Nextcloud
    # This prevents Q1 provisioning from making Q2 look done.
    nc_provisioned = False
    if distro_exists:
        try:
            r = subprocess.run(
                ["sudo", "-u", "www-data", "php", str(NC_OCC),
                 "user:list", "--output=json"],
                capture_output=True, text=True, timeout=10)
            users = json.loads(r.stdout)
            client_usernames = [v.get("username") for v in clients.values()]
            nc_provisioned = any(u in users for u in client_usernames if u)
        except:
            pass

    # Determine overall pipeline stage
    all_downloaded = downloaded_count == total
    any_downloaded = downloaded_count > 0
    all_ingested   = all(s["ingested"] for s in dataset_status.values())

    if all_ingested and nc_provisioned:
        stage = "complete"
    elif all_ingested:
        stage = "ready_to_provision"
    elif all_downloaded and distro_exists:
        stage = "ready_to_ingest"
    elif any_downloaded and distro_exists:
        stage = "downloading"
    elif any_downloaded and not distro_exists:
        stage = "downloading"   # downloading but structure not created yet
    elif distro_exists:
        stage = "structure_ready"
    else:
        stage = "not_started"

    return {
        "quarter":          quarter,
        "version":          version,
        "distro_folder":    distro,
        "distro_exists":    distro_exists,
        "datasets":         dataset_status,
        "total_datasets":   total,
        "downloaded_count": downloaded_count,
        "clients":          len(clients),
        "nc_provisioned":   nc_provisioned,
        "stage":            stage,
        "active_download":  active_download,
        "next_action":      get_next_action(stage, dataset_status, distro_exists),
    }


def get_next_action(stage, datasets, distro_exists):
    """Return a plain-English next action for the user."""
    pending  = [k for k, v in datasets.items() if not v["downloaded"]]
    ready    = [k for k, v in datasets.items() if v["downloaded"] and not v["ingested"]]
    ingested = [k for k, v in datasets.items() if v["ingested"]]

    if stage == "complete":
        return {
            "action": "done",
            "label": "Delivery complete!",
            "description": f"All {len(ingested)} datasets are organised and clients have access to their data in Nextcloud."}

    if stage == "ready_to_provision":
        return {
            "action": "provision",
            "label": "Step 5 — Give clients access in Nextcloud",
            "description": f"All data is organised into the distribution folders. Click to update client accounts and folder access in Nextcloud."}

    if stage == "ready_to_ingest":
        return {
            "action": "ingest",
            "label": "Step 4 — Organise files into distribution folders",
            "description": f"{len(ready)} dataset(s) downloaded and ready. Click to copy files into the correct folder structure. Always preview first."}

    if stage == "downloading":
        downloaded = [k for k, v in datasets.items() if v["downloaded"]]
        return {
            "action": "download",
            "label": f"Step 2 — Continue downloading ({len(pending)} dataset(s) still needed)",
            "description": (
                f"Downloaded so far: {', '.join(downloaded) if downloaded else 'none'}. "
                f"Still needed: {', '.join(pending[:4])}{'...' if len(pending) > 4 else ''}. "
                f"When all downloads are done, the Organise button will appear automatically.")}

    if stage == "structure_ready":
        return {
            "action": "download",
            "label": "Step 2 — Download data from TomTom",
            "description": "Folder structure is ready. Start downloading each product. You can download one at a time — the dashboard updates automatically as each one completes."}

    return {
        "action": "create_structure",
        "label": "Step 1 — Create the distribution folder structure",
        "description": "Before downloading anything, you need to create the empty folder structure for this quarter. This takes about 10 seconds."}


def get_all_quarters():
    quarters = set()
    for d in DISTRO_BASE.glob("DISTRIBUTION_*"):
        if d.is_dir():
            quarters.add(d.name.replace("DISTRIBUTION_", ""))
    for f in QUARTERLY.glob("*.yaml"):
        quarters.add(f.stem)
    return sorted(quarters, reverse=True)


def suggest_next_quarter(existing):
    if not existing:
        now = datetime.now()
        return f"Q{(now.month-1)//3+1}_{now.year}"
    latest = sorted(existing)[-1]
    m = re.match(r"Q(\d)_(\d{4})", latest)
    if not m:
        return None
    q, y = int(m.group(1)), int(m.group(2))
    q += 1
    if q > 4:
        q, y = 1, y + 1
    return f"Q{q}_{y}"


def get_mapflow_status():
    status = {"running": False, "processes": [], "recent_logs": [], "downloads": []}
    try:
        r = subprocess.run(["pgrep", "-a", "-f", "mapflow.main"],
                           capture_output=True, text=True)
        if r.stdout.strip():
            status["running"] = True
            for line in r.stdout.strip().split("\n"):
                if line:
                    parts = line.split(None, 1)
                    status["processes"].append({"pid": parts[0], "cmd": parts[1] if len(parts) > 1 else ""})
    except:
        pass
    try:
        for lf in sorted(LOGS_DIR.glob("download_*.log"),
                         key=lambda f: f.stat().st_mtime, reverse=True)[:3]:
            lines = [l for l in lf.read_text().strip().split("\n")[-20:] if l.strip()]
            age   = datetime.now().timestamp() - lf.stat().st_mtime
            status["recent_logs"].append({
                "name": lf.name, "last_lines": lines,
                "age_mins": round(age / 60), "active": age < 300})
    except:
        pass
    try:
        if DOWNLOADS.exists():
            for d in sorted(DOWNLOADS.iterdir(),
                            key=lambda x: x.stat().st_mtime, reverse=True)[:10]:
                if d.is_dir():
                    try:
                        r = subprocess.run(["du", "-sh", str(d)],
                                           capture_output=True, text=True, timeout=5)
                        size = r.stdout.split()[0]
                    except:
                        size = "?"
                    age = datetime.now().timestamp() - d.stat().st_mtime
                    status["downloads"].append({
                        "name": d.name, "size": size,
                        "age_mins": round(age / 60), "active": age < 600})
    except:
        pass
    return status


def get_system_status():
    status = {}
    for svc in ["apache2", "mariadb", "redis-server", "nextcloud-refresh"]:
        try:
            r = subprocess.run(["systemctl", "is-active", svc],
                               capture_output=True, text=True)
            status[svc.replace("-", "_")] = r.stdout.strip() == "active"
        except:
            status[svc.replace("-", "_")] = False
    try:
        r = subprocess.run(["sudo", "-u", "www-data", "php", str(NC_OCC),
                            "status", "--output=json"],
                           capture_output=True, text=True, timeout=10)
        nc = json.loads(r.stdout)
        status["nextcloud"]      = nc.get("installed", False)
        status["nc_version"]     = nc.get("versionstring", "?")
        status["nc_maintenance"] = nc.get("maintenance", False)
    except:
        status["nextcloud"] = False
        status["nc_version"] = "?"
        status["nc_maintenance"] = False
    try:
        r = subprocess.run(["df", "-h", "/mnt/data"],
                           capture_output=True, text=True)
        parts = r.stdout.strip().split("\n")[1].split()
        status["disk_used"]  = parts[2]
        status["disk_avail"] = parts[3]
        status["disk_pct"]   = parts[4]
    except:
        status["disk_used"] = status["disk_avail"] = status["disk_pct"] = "?"
    try:
        r = subprocess.run(["sudo", "-u", "www-data", "php", str(NC_OCC),
                            "user:list", "--output=json"],
                           capture_output=True, text=True, timeout=10)
        users = json.loads(r.stdout)
        status["client_count"] = len([u for u in users if u != "admin"])
    except:
        status["client_count"] = "?"
    status["nc_url"] = nc_url()
    return status


def read_env_file(path):
    result = []
    try:
        for line in Path(path).read_text().split("\n"):
            s = line.strip()
            if s.startswith("#") or s == "":
                result.append({"type": "comment", "raw": line})
            elif "=" in s:
                key, _, val = s.partition("=")
                result.append({"type": "var", "key": key.strip(), "value": val.strip()})
            else:
                result.append({"type": "comment", "raw": line})
    except:
        pass
    return result


def write_env_file(path, entries):
    lines = []
    for e in entries:
        if e.get("type") == "comment":
            lines.append(e.get("raw", ""))
        else:
            lines.append(f"{e['key']}={e['value']}")
    Path(path).write_text("\n".join(lines) + "\n")


def stream_command(cmd, job_id, env=None, cwd=None, notify_on_done=None):
    running_jobs[job_id] = {
        "status": "running", "output": [],
        "started": datetime.now().isoformat()}
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=full_env,
            cwd=str(cwd) if cwd else None)
        for line in proc.stdout:
            running_jobs[job_id]["output"].append(line.rstrip())
        proc.wait()
        running_jobs[job_id]["status"] = "done" if proc.returncode == 0 else "error"
        running_jobs[job_id]["returncode"] = proc.returncode

        # Send email notification if configured
        if notify_on_done and proc.returncode == 0:
            subject, body = notify_on_done
            send_notification_email(subject, body)

    except Exception as e:
        running_jobs[job_id]["output"].append(f"ERROR: {e}")
        running_jobs[job_id]["status"] = "error"


def run_bg(cmd, job_id, env=None, cwd=None, notify_on_done=None):
    t = threading.Thread(
        target=stream_command,
        args=(cmd, job_id, env, cwd, notify_on_done), daemon=True)
    t.start()
    return job_id


# ── Routes ────────────────────────────────────────────────────
@app.route("/")
def index():
    quarters = get_all_quarters()
    resp = make_response(render_template("index.html",
        quarters=quarters,
        next_quarter=suggest_next_quarter(quarters),
        nc_url=nc_url()))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/api/status")
def api_status():
    return jsonify(get_system_status())


@app.route("/api/quarters")
def api_quarters():
    quarters = get_all_quarters()
    next_q   = suggest_next_quarter(quarters)
    details  = {}
    for q in quarters:
        p = DISTRO_BASE / f"DISTRIBUTION_{q}"
        if p.exists():
            try:
                r = subprocess.run(["du", "-sh", str(p)],
                                   capture_output=True, text=True, timeout=10)
                size = r.stdout.split()[0]
            except:
                size = "?"
            details[q] = {
                "exists": True, "size": size,
                "products": [d.name for d in p.iterdir() if d.is_dir()]}
        else:
            details[q] = {"exists": False, "size": "0", "products": []}
    return jsonify({"quarters": quarters, "next": next_q, "details": details})


@app.route("/api/verify/<family>/<region>")
def api_verify_download(family, region):
    """
    Verify a download against TomTom's expected manifest. Returns a plain
    summary of how complete the download actually is, independent of the
    dashboard's "already downloaded" folder-existence check.
    """
    version = request.args.get("version", "")
    if not version:
        return jsonify({"error": "version query param required"}), 400
    pattern_fn = FOLDER_PATTERNS.get(family)
    if not pattern_fn:
        return jsonify({"error": f"Unknown family: {family}"}), 400
    folder_name = pattern_fn(region, version)
    full_path = DOWNLOADS / folder_name
    if not full_path.exists():
        return jsonify({"error": f"Download folder not found: {folder_name}"}), 404
    result = verify_download_against_manifest(full_path, folder_name)
    result["folder_name"] = folder_name
    return jsonify(result)


@app.route("/api/pipeline/<quarter>")
def api_pipeline(quarter):
    status = get_pipeline_status(quarter)
    if not status:
        return jsonify({"error": "Quarter config not found"}), 404
    return jsonify(status)


@app.route("/api/mapflow")
def api_mapflow():
    return jsonify(get_mapflow_status())


@app.route("/api/job/<job_id>")
def job_status(job_id):
    return jsonify(running_jobs.get(job_id, {"status": "not_found"}))


# ── Config ────────────────────────────────────────────────────
@app.route("/api/config/env", methods=["GET"])
def get_config_env():
    return jsonify(read_env_file(CONFIG_ENV))

@app.route("/api/config/env", methods=["POST"])
def save_config_env():
    write_env_file(CONFIG_ENV, request.json.get("entries", []))
    return jsonify({"ok": True})

@app.route("/api/config/folders", methods=["GET"])
def get_config_folders():
    return jsonify(read_env_file(FOLDERS_ENV))

@app.route("/api/config/folders", methods=["POST"])
def save_config_folders():
    write_env_file(FOLDERS_ENV, request.json.get("entries", []))
    return jsonify({"ok": True})

@app.route("/api/config/quarterly/<quarter>", methods=["GET"])
def get_quarterly(quarter):
    path = QUARTERLY / f"{quarter}.yaml"
    if not path.exists():
        return jsonify({"error": "not found"}), 404
    return jsonify({"content": path.read_text()})

@app.route("/api/config/quarterly/<quarter>", methods=["POST"])
def save_quarterly(quarter):
    path = QUARTERLY / f"{quarter}.yaml"
    QUARTERLY.mkdir(exist_ok=True)
    path.write_text(request.json.get("content", ""))
    return jsonify({"ok": True})


# ── Generate .folders.env ─────────────────────────────────────
@app.route("/api/run/generate_env", methods=["POST"])
def run_generate_env():
    quarter = request.json.get("quarter", "Q1_2026")
    dry_run = request.json.get("dry_run", False)
    job_id  = f"genenv_{datetime.now().strftime('%H%M%S')}"
    cmd = [sys.executable, str(SCRIPTS / "generate_env.py"),
           "--quarter", quarter]
    if dry_run:
        cmd.append("--dry-run")
    run_bg(cmd, job_id)
    return jsonify({"job_id": job_id})


# ── Clients ───────────────────────────────────────────────────
@app.route("/api/clients")
def get_clients():
    try:
        r = subprocess.run(
            ["sudo", "-u", "www-data", "php", str(NC_OCC),
             "user:list", "--output=json"],
            capture_output=True, text=True, timeout=10)
        users = json.loads(r.stdout)
        result = []
        for k, v in users.items():
            if k == "admin":
                continue
            try:
                r2 = subprocess.run(
                    ["sudo", "-u", "www-data", "php", str(NC_OCC),
                     "user:info", k, "--output=json"],
                    capture_output=True, text=True, timeout=10)
                info = json.loads(r2.stdout)
                result.append({
                    "username": k, "display": v,
                    "groups":   info.get("groups", []),
                    "enabled":  info.get("enabled", True),
                    "last_seen": info.get("last_seen", "never")})
            except:
                result.append({
                    "username": k, "display": v,
                    "groups": [], "enabled": True, "last_seen": "never"})
        return jsonify(result)
    except:
        return jsonify([])



@app.route("/api/clients/available-datasets")
def available_datasets():
    import yaml as _yaml
    datasets = []
    quarter = sorted(get_all_quarters() or ["Q2_2026"], reverse=True)[0]
    try:
        q_file = QUARTERLY / f"{quarter}.yaml"
        if q_file.exists():
            with open(q_file) as f:
                cfg = _yaml.safe_load(f)
            distro = cfg.get("distro_folder", "")
            dataset_labels = {
                "MNR_MEA":"MultiNet-R Africa","MNR_EUR":"MultiNet-R Europe",
                "MNR_SOUTHERN_AFRICA":"MultiNet-R Southern Africa","MNR_ZAF":"MultiNet-R South Africa",
                "MN_MEA":"MultiNet Africa","MN_EUR":"MultiNet Europe","MN_3D_TRACKING":"MultiNet 3D Tracking",
                "SP_MEA":"Speed Profiles Africa","SP_EUR":"Speed Profiles Europe",
                "POI_MEA":"Premium POI Africa","POI_ZAF":"Premium POI South Africa",
                "POI_SOUTHERN_AFRICA":"Premium POI Southern Africa",
                "APT_MEA":"Address Points Africa","MAPIT_ZAF":"MapIT South Africa","MAPIT_AFR":"MapIT Africa",
            }
            distro_paths = {
                "MNR_MEA":"MNR/MNR_MEA","MNR_EUR":"MNR/MNR_EUR",
                "MNR_SOUTHERN_AFRICA":"MNR/MNR_SOUTHERN_AFRICA","MNR_ZAF":"MNR/MNR_ZAF",
                "MN_MEA":"MN/MN_MEA","MN_EUR":"MN/MN_EUR","MN_3D_TRACKING":"MN/MN_3D_TRACKING",
                "SP_MEA":"PRODUCTS/SPEED_PROFILES/SPEED_PROFILES_MEA",
                "SP_EUR":"PRODUCTS/SPEED_PROFILES/SPEED_PROFILES_EUR",
                "POI_MEA":"PRODUCTS/MN_PREMIUM_POI/MN_PREMIUM_POI_AFR",
                "POI_ZAF":"PRODUCTS/MN_PREMIUM_POI/MN_PREMIUM_POI_ZAF",
                "POI_SOUTHERN_AFRICA":"PRODUCTS/MN_PREMIUM_POI/MN_PREMIUM_POI_SOUTHERN_AFRICA",
                "APT_MEA":"PRODUCTS/MN_APT/MN_APT_MEA",
            }
            for key, label in dataset_labels.items():
                rel = distro_paths.get(key,"")
                path = Path(distro)/rel if distro and rel else None
                has_data = False
                if path and path.exists():
                    for pat in ["*.tar.gz","*.7z.001"]:
                        if any(True for _ in path.rglob(pat)):
                            has_data = True; break
                datasets.append({"key":key,"label":label,"path":str(path) if path else "","available":has_data,"custom":False})
    except: pass
    custom_base = Path("/mnt/data/custom")
    if custom_base.exists():
        for d in sorted(custom_base.iterdir()):
            if d.is_dir():
                datasets.append({"key":f"CUSTOM_{d.name.upper()}","label":d.name.replace("_"," ").title(),"path":str(d),"available":True,"custom":True})
    return jsonify({"datasets":datasets,"quarter":quarter})


@app.route("/api/clients/add", methods=["POST"])
def add_client():
    data     = request.json
    display  = data.get("display_name", "").strip()
    username = data.get("username", "").strip().lower().replace(" ", "_")
    password = data.get("password") or str(uuid.uuid4())[:16]
    datasets = data.get("datasets", [])
    quarter  = data.get("quarter", sorted(get_all_quarters() or ["Q2_2026"], reverse=True)[0])
    job_id   = f"addclient_{datetime.now().strftime('%H%M%S')}"

    def _add():
        running_jobs[job_id] = {
            "status": "running", "output": [],
            "started": datetime.now().isoformat()}

        def log(msg):
            running_jobs[job_id]["output"].append(msg)

        log(f"Creating user: {username}")
        log(f"Display name: {display}")
        log("")

        env = os.environ.copy()
        env["OC_PASS"] = password
        try:
            r = subprocess.run(
                ["sudo", "-E", "-u", "www-data", "php", str(NC_OCC),
                 "user:add", "--password-from-env",
                 f"--display-name={display}", username],
                capture_output=True, text=True, timeout=30, env=env)
            output = (r.stdout + r.stderr).strip()
        except Exception as e:
            output = str(e)
            r = type("R", (), {"returncode": 1})()

        if r.returncode == 0 or "successfully created" in output.lower():
            log("✅ User account created")
        else:
            log(f"❌ Failed to create user")
            log(f"   Error: {output}")
            log("")
            log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            log("❌ Client creation FAILED")
            log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            running_jobs[job_id]["status"] = "error"
            return

        # ── Mount datasets ────────────────────────────────────
        csv_rows = []
        if datasets:
            log(""); log("Setting up data access...")
            for ds in datasets:
                key = ds.get("key",""); label = ds.get("label",key)
                path = ds.get("path",""); is_custom = ds.get("custom",False)
                folder_name = ds.get("folder_name","")
                if is_custom and folder_name:
                    slug = folder_name.lower().replace(" ","_").replace("-","_")
                    path = f"/mnt/data/custom/{slug}"
                    Path(path).mkdir(parents=True, exist_ok=True)
                    log(f"  📁 Created: {path}")
                    log(f"     ℹ️  Ask a technician to place data in: {path}")
                if not path: continue
                grp = f"{username}_{key.lower()}"
                subprocess.run(["sudo","-u","www-data","php",str(NC_OCC),"group:add",grp],capture_output=True,text=True)
                mount_label = label.replace(" ","_")
                r_mount = subprocess.run(["sudo","-u","www-data","php",str(NC_OCC),
                    "files_external:create",f"/{mount_label}","local","null::null",
                    "--config",f"datadir={path}","--group",grp],capture_output=True,text=True)
                subprocess.run(["sudo","-u","www-data","php",str(NC_OCC),"group:adduser",grp,username],capture_output=True,text=True)
                log(f"  {'✅' if r_mount.returncode==0 else '⚠️ '} {label}")
                csv_rows.append((label,path))
            subprocess.run(["sudo","-u","www-data","php",str(NC_OCC),"files:scan",username],capture_output=True,text=True,timeout=60)
            log("  ✅ Nextcloud scanned")
            import csv as _csv
            csv_path = DATA_DIR/"client_distribution.csv"
            if csv_rows and csv_path.exists():
                with open(csv_path,"a",newline="") as _cf:
                    _w = _csv.writer(_cf)
                    for lbl,pth in csv_rows:
                        _w.writerow([display or username,"",lbl,lbl.lower().replace(" ","_"),pth])
            try:
                import yaml as _yaml
                q_file = QUARTERLY/f"{quarter}.yaml"
                if q_file.exists():
                    with open(q_file) as _qf: cfg = _yaml.safe_load(_qf)
                    if "clients" not in cfg: cfg["clients"]={}
                    cname = display or username
                    if cname not in cfg["clients"]:
                        cfg["clients"][cname]={"username":username,"datasets":[ds.get("key") for ds in datasets if ds.get("key")]}
                        with open(q_file,"w") as _qf: _yaml.dump(cfg,_qf,default_flow_style=False,allow_unicode=True)
                    log("  ✅ Added to notifications")
            except Exception as _ye: log(f"  ⚠️  YAML: {_ye}")

        r_check = subprocess.run(
            ["sudo", "-u", "www-data", "php", str(NC_OCC),
             "user:info", username, "--output=json"],
            capture_output=True, text=True, timeout=10)

        log("")
        log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        if r_check.returncode == 0:
            log("✅ CLIENT CREATED SUCCESSFULLY")
            log("")
            log(f"  Username : {username}")
            log(f"  Password : {password}")
            log(f"  URL      : {nc_url()}")
            log("")
            log("  ⚠️  Save this password — it won't be shown again.")
            log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            running_jobs[job_id]["status"] = "done"
            running_jobs[job_id]["credentials"] = {
                "username": username, "password": password}
            save_password_to_csv(display or username, username, password)
        else:
            log("❌ Verification failed — user may not have been created")
            log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            running_jobs[job_id]["status"] = "error"

    threading.Thread(target=_add, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/groups")
def get_groups():
    try:
        r = subprocess.run(
            ["sudo", "-u", "www-data", "php", str(NC_OCC),
             "group:list", "--output=json"],
            capture_output=True, text=True, timeout=10)
        return jsonify(sorted(json.loads(r.stdout).keys()))
    except:
        return jsonify([])


@app.route("/api/clients/disable", methods=["POST"])
def disable_client():
    username = request.json.get("username")
    job_id   = f"disable_{datetime.now().strftime('%H%M%S')}"
    run_bg(["sudo", "-u", "www-data", "php", str(NC_OCC),
            "user:disable", username], job_id)
    return jsonify({"job_id": job_id})


@app.route("/api/clients/enable", methods=["POST"])
def enable_client():
    username = request.json.get("username")
    job_id   = f"enable_{datetime.now().strftime('%H%M%S')}"
    run_bg(["sudo", "-u", "www-data", "php", str(NC_OCC),
            "user:enable", username], job_id)
    return jsonify({"job_id": job_id})


# ── Pipeline actions ──────────────────────────────────────────
@app.route("/api/run/healthcheck", methods=["POST"])
def run_healthcheck():
    job_id = f"health_{datetime.now().strftime('%H%M%S')}"

    def _check():
        running_jobs[job_id] = {"status": "running", "output": [], "started": datetime.now().isoformat()}
        def log(m): running_jobs[job_id]["output"].append(m)

        log("━━━  System Health Check  ━━━")
        log(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        log("")
        log("── Services ──")
        for svc in ["apache2", "mariadb", "redis-server", "nextcloud-refresh", "ddq-ui"]:
            try:
                r = subprocess.run(["systemctl", "is-active", svc], capture_output=True, text=True)
                ok = r.stdout.strip() == "active"
                log(f"  {'✅' if ok else '❌'} {svc}: {r.stdout.strip()}")
            except Exception as e:
                log(f"  ⚠️  {svc}: {e}")
        log("")
        log("── Nextcloud ──")
        try:
            r = subprocess.run(["sudo", "-u", "www-data", "php", str(NC_OCC), "status", "--output=json"], capture_output=True, text=True, timeout=10)
            nc = json.loads(r.stdout)
            log(f"  {'✅' if nc.get('installed') else '❌'} Installed: {nc.get('installed')}")
            log(f"  {'⚠️ ' if nc.get('maintenance') else '✅'} Maintenance: {nc.get('maintenance')}")
            log(f"  ℹ️  Version: {nc.get('versionstring','?')}")
        except Exception as e:
            log(f"  ❌ {e}")
        log("")
        log("── Disk space ──")
        try:
            r = subprocess.run(["df", "-h", "/mnt/data"], capture_output=True, text=True)
            parts = r.stdout.strip().split("\n")[1].split()
            pct = int(parts[4].replace("%",""))
            log(f"  {'✅' if pct<80 else '⚠️ '} /mnt/data: {parts[2]} used ({parts[4]}) — {parts[3]} free")
        except Exception as e:
            log(f"  ❌ {e}")
        log("")
        log("── Downloads ──")
        try:
            if DOWNLOADS.exists():
                count = sum(1 for d in DOWNLOADS.iterdir() if d.is_dir())
                log(f"  ✅ {DOWNLOADS}: {count} folder(s)")
            else:
                log(f"  ⚠️  Downloads folder not found: {DOWNLOADS}")
        except Exception as e:
            log(f"  ⚠️  {e}")
        log("")
        log("── Distribution folders ──")
        try:
            distros = sorted(DISTRO_BASE.glob("DISTRIBUTION_*"), reverse=True)
            if distros:
                for d in [x for x in distros[:5] if x.name != "DISTRIBUTION_CURRENT"]:
                    r = subprocess.run(["du", "-sh", str(d)], capture_output=True, text=True, timeout=15)
                    size = r.stdout.split()[0] if r.stdout else "?"
                    log(f"  ✅ {d.name}: {size}")
            else:
                log("  ⚠️  No DISTRIBUTION_* folders found")
        except Exception as e:
            log(f"  ⚠️  {e}")
        log("")
        log("── Firewall ──")
        try:
            r = subprocess.run(["ufw", "status"], capture_output=True, text=True)
            active = "active" in r.stdout.lower()
            log(f"  {'✅' if active else '❌'} UFW: {'active' if active else 'inactive'}")
            for port in ["22", "80", "443", "5050"]:
                log(f"  {'✅' if port in r.stdout else '⚠️ '} Port {port}")
        except Exception as e:
            log(f"  ⚠️  {e}")
        log("")
        log("━━━  Health check complete  ━━━")
        running_jobs[job_id]["status"] = "done"

    threading.Thread(target=_check, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/run/create_structure", methods=["POST"])
def run_create_structure():
    quarter = request.json.get("quarter", "Q1_2026")
    job_id  = f"structure_{datetime.now().strftime('%H%M%S')}"
    run_bg(["bash", str(SCRIPTS / "create_structure.sh"),
            "--quarter", quarter], job_id,
           notify_on_done=(
               f"✅ GeoInt: Q{quarter} folder structure created",
               f"<p>The distribution folder structure for <strong>{quarter}</strong> has been created on the server.</p><p>Next step: start downloading data from TomTom.</p>"))
    return jsonify({"job_id": job_id})


@app.route("/api/run/download", methods=["POST"])
def run_download():
    family   = request.json.get("family", "MNR")
    quarter  = request.json.get("quarter", "Q1_2026")
    version  = request.json.get("version", "")
    region   = request.json.get("region", "MEA")
    force    = request.json.get("force", False)
    job_id   = f"download_{family}_{region}_{datetime.now().strftime('%H%M%S')}"
    log_file = LOGS_DIR / f"download_{family}_{region}_{quarter}.log"
    # Guard — block if mapflow already running
    try:
        _pgrep = subprocess.run(["pgrep", "-f", "mapflow.main"], capture_output=True, text=True)
        if _pgrep.stdout.strip():
            return jsonify({"error": "already_running", "message": "A download is already in progress. Wait for it to complete before starting another."}), 409
    except:
        pass

    # Backend guard — check if already downloaded and force not set
    if not force:
        already, folder, size, incomplete_count = check_dataset_downloaded(
            f"{family}_{region}", family, region, version)
        if already:
            return jsonify({
                "error": "already_downloaded",
                "message": (f"{family} {region} data already exists on the server "
                            f"({folder}, {size}). Tick the checkbox to download again.")
            }), 409

    # Auto-update download.sh with correct values
    # so the user never has to edit it manually
    download_sh = MAPFLOW / "download.sh"
    if download_sh.exists():
        try:
            import re as _re
            sh_content = download_sh.read_text()
            sh_content = _re.sub(
                r'^QUARTER=.*$', f'QUARTER="{quarter}"',
                sh_content, flags=_re.MULTILINE)
            if version:
                sh_content = _re.sub(
                    r'^TOMTOM_VERSION=.*$', f'TOMTOM_VERSION="{version}"',
                    sh_content, flags=_re.MULTILINE)
            sh_content = _re.sub(
                r'^FAMILY=.*$', f'FAMILY="{family}"',
                sh_content, flags=_re.MULTILINE)
            # Update regions — uncomment the matching region, comment others
            # Replace active REGIONS block with the selected region
            pass  # region passed via env var
            download_sh.write_text(sh_content)
        except Exception as e:
            pass  # Non-fatal

    def _run_download():
        """Run download and verify success by checking folder exists on disk."""
        running_jobs[job_id] = {
            "status": "running", "output": [],
            "started": datetime.now().isoformat()}

        def log(m):
            running_jobs[job_id]["output"].append(m)

        full_env = os.environ.copy()
        full_env.update({
            "MAPFLOW_FAMILY":  family,
            "MAPFLOW_QUARTER": quarter,
            "MAPFLOW_VERSION": version,
            "MAPFLOW_REGION":  region,
        })

        try:
            proc = subprocess.Popen(
                ["bash", "-c", f"bash {MAPFLOW}/download.sh 2>&1 | tee {log_file}"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, env=full_env)
            for line in proc.stdout:
                running_jobs[job_id]["output"].append(line.rstrip())
            proc.wait()
        except Exception as e:
            log(f"ERROR: {e}")
            running_jobs[job_id]["status"] = "error"
            return

        # Don't rely on exit code alone — verify folder actually exists on disk
        # Some download tools (aria2c) return non-zero even on success
        _fname = f"MultiNet-{'-R' if family == 'MNR' else ''}{family if family != 'MNR' else ''}_{region}_{version}_full_commercial"
        expected_folder = DOWNLOADS_RAW / _fname if (DOWNLOADS_RAW / _fname).exists() else DOWNLOADS / _fname

        # Try multiple folder name patterns
        folder_exists = False
        found_folder  = None
        patterns = [
            f"MultiNet-R_{region}_{version}_full_commercial",         # MNR
            f"MultiNet_{region}_{version}_full_commercial",            # MN
            f"MultiNet-SpeedProfile_{region}_{version}_full_commercial", # MNSP
            f"MultiNet-POI_{region}_{version}_full_commercial",        # MNPOI
            f"MultiNet-AddressPoints_{region}_{version}_full_commercial", # MNAP
        ]
        for pattern in patterns:
            candidate = DOWNLOADS_RAW / pattern if (DOWNLOADS_RAW / pattern).exists() else DOWNLOADS / pattern
            if candidate.exists():
                try:
                    has_data = any(True for _ in candidate.rglob("*.tar.gz"))
                    if not has_data:
                        has_data = sum(1 for _ in candidate.rglob("*") if _.is_file()) > 5
                    if has_data:
                        folder_exists = True
                        found_folder  = candidate
                        break
                except:
                    pass

        if folder_exists:
            log("")
            log(f"Folder found: {found_folder.name} — verifying against TomTom manifest...")
            running_jobs[job_id]["status"] = "done"

            # Verify against TomTom's manifest before claiming success.
            # A folder existing with some files is not proof the download
            # actually finished — disk-space failures and connection drops
            # can leave a folder that looks present but is missing most of
            # its files. Only send the "complete" email when verification
            # confirms every expected file is present at the right size.
            verify_result = None
            try:
                verify_result = verify_download_against_manifest(found_folder, found_folder.name)
            except Exception as _ve:
                log(f"⚠️  Could not verify against manifest: {_ve}")

            if verify_result and verify_result.get("verified") and verify_result.get("complete"):
                log(f"✅ Verified complete: {verify_result['present_files']}/{verify_result['expected_files']} files, "
                    f"{verify_result['expected_size_gb']}GB")
                send_notification_email(
                    f"✅ GeoInt: {family} {region} download complete for {quarter}",
                    f"<p><strong>{family} {region}</strong> data for <strong>{quarter}</strong> "
                    f"has finished downloading successfully and was verified against "
                    f"TomTom's file manifest ({verify_result['expected_files']} files, "
                    f"{verify_result['expected_size_gb']}GB).</p>"
                    f"<p>Folder: {found_folder.name}</p>"
                    f"<p>Go to the dashboard and click <strong>Organise files</strong> "
                    f"when all downloads are complete.</p>"
                    f"<p><a href='https://{load_env().get('NC_DOMAIN','localhost')}:5050'>"
                    f"Open Dashboard</a></p>")
            elif verify_result and verify_result.get("verified") and not verify_result.get("complete"):
                pct = verify_result["percent_complete"]
                missing_gb = verify_result["missing_size_gb"]
                missing_n = verify_result["missing_files_count"]
                log(f"⚠️  INCOMPLETE: only {pct}% done — {missing_n} file(s) / {missing_gb}GB still missing")
                send_notification_email(
                    f"⚠️ GeoInt: {family} {region} download STOPPED EARLY for {quarter} — only {pct}% complete",
                    f"<p><strong>{family} {region}</strong> data for <strong>{quarter}</strong> "
                    f"stopped before finishing. Only <strong>{pct}%</strong> of the expected data "
                    f"downloaded ({missing_gb}GB / {missing_n} file(s) still missing).</p>"
                    f"<p>This is NOT ready to organise or deliver to clients yet.</p>"
                    f"<p>Open the dashboard Pipeline page — the dataset card will show "
                    f"a <strong>Finish downloading</strong> button to complete it.</p>"
                    f"<p><a href='https://{load_env().get('NC_DOMAIN','localhost')}:5050'>"
                    f"Open Dashboard</a></p>")
            else:
                # No manifest available to verify against — fall back to the
                # old folder-existence signal but say so plainly, rather than
                # implying a confidence level we don't actually have.
                log("⚠️  No TomTom manifest available — could not verify completeness")
                send_notification_email(
                    f"⚠️ GeoInt: {family} {region} download finished for {quarter} — NOT independently verified",
                    f"<p><strong>{family} {region}</strong> data for <strong>{quarter}</strong> "
                    f"finished running and files are present, but this could not be checked "
                    f"against TomTom's file manifest (none was found).</p>"
                    f"<p>Please double-check the folder size looks correct before organising "
                    f"or delivering this to clients.</p>"
                    f"<p>Folder: {found_folder.name}</p>"
                    f"<p><a href='https://{load_env().get('NC_DOMAIN','localhost')}:5050'>"
                    f"Open Dashboard</a></p>")
        else:
            # Exit code failure — but check if it's a partial/real failure
            if proc.returncode != 0:
                log("")
                log(f"⚠️  Download process exited with code {proc.returncode}")
                log(f"    The folder was not found at expected location.")
                log(f"    Check the output above for errors.")
                running_jobs[job_id]["status"] = "error"
            else:
                log("")
                log(f"⚠️  Process succeeded but folder not found — check downloads manually")
                running_jobs[job_id]["status"] = "error"

    threading.Thread(target=_run_download, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/run/download_mapit", methods=["POST"])
def run_download_mapit():
    quarter  = request.json.get("quarter", "Q1_2026")
    job_id   = f"mapit_{datetime.now().strftime('%H%M%S')}"
    log_file = LOGS_DIR / f"download_mapit_{quarter}.log"
    run_bg(["bash", "-c",
            f"bash {MAPFLOW}/download_mapit.sh 2>&1 | tee {log_file}"],
           job_id,
           notify_on_done=(
               f"✅ GeoInt: MAPIT download complete for {quarter}",
               f"<p>MAPIT data for <strong>{quarter}</strong> has finished downloading.</p>"))
    return jsonify({"job_id": job_id})




# ── Source dependency map ──────────────────────────────────────
# Defines which datasets share a source folder and the order
# they must be ingested before the source can be deleted.
# Format: source_folder_key -> [dataset_keys in order, last=main]
SOURCE_DEPENDENCY_ORDER = {
    "MNR_MEA_SOURCE": ["MNR_ZAF", "MNR_ZAF_PLUS", "MNR_SOUTHERN_AFRICA", "MNR_MEA"],
    "MNR_EUR_SOURCE": ["MNR_EUR"],
    "MN_MEA_SOURCE":  ["MN_MEA"],
    "MN_EUR_SOURCE":  ["MN_3D_TRACKING", "MN_EUR"],
}


def get_source_folder(key, version):
    """Get the download source folder for a dataset key."""
    subset_map = {
        "MNR_ZAF":             ("MNR", "MEA"),
        "MNR_ZAF_PLUS":        ("MNR", "MEA"),
        "MNR_SOUTHERN_AFRICA": ("MNR", "MEA"),
        "MNR_MEA":             ("MNR", "MEA"),
        "MNR_EUR":             ("MNR", "EUR"),
        "MN_MEA":              ("MN",  "MEA"),
        "MN_EUR":              ("MN",  "EUR"),
        "MN_3D_TRACKING":      ("MN",  "EUR"),
        "MN_AFR":              ("MN",  "MEA"),
    }
    if key in subset_map:
        family, region = subset_map[key]
    else:
        prefix = key.split("_")[0]
        region = key.replace(f"{prefix}_", "").split("_")[0]
        family = prefix
    pattern_fn = FOLDER_PATTERNS.get(family)
    if not pattern_fn:
        return None
    return DOWNLOADS / pattern_fn(region, version)


def all_siblings_ingested(key, version, distro):
    """
    Check if all configured datasets sharing the same source as this key
    have been ingested. If so, the source can be safely deleted.

    Important: SOURCE_DEPENDENCY_ORDER may include datasets that do not exist
    in the selected quarter. Those must not block source cleanup.
    """
    source = get_source_folder(key, version)
    if not source:
        return False, []

    configured_keys = None
    try:
        quarter_name = distro.name.replace("DISTRIBUTION_", "")
        cfg_path = QUARTERLY / f"{quarter_name}.yaml"
        if cfg_path.exists():
            with open(cfg_path) as f:
                cfg = yaml.safe_load(f) or {}
            configured_keys = set((cfg.get("datasets") or {}).keys())
    except Exception:
        configured_keys = None

    # Find all configured datasets that share this source.
    siblings = []
    key_source = get_source_folder(key, version)
    for group in SOURCE_DEPENDENCY_ORDER.values():
        for sibling_key in group:
            if configured_keys is not None and sibling_key not in configured_keys:
                continue
            sib_source = get_source_folder(sibling_key, version)
            if sib_source == key_source and sibling_key not in siblings:
                siblings.append(sibling_key)

    if not siblings:
        siblings = [key]

    # Check each sibling destination has real data.
    not_ingested = []
    for sib in siblings:
        prefix  = sib.split("_")[0]
        region  = sib.replace(f"{prefix}_", "")
        dest_map = {
            "MNR": distro / "MNR" / f"MNR_{region}",
            "MN":  distro / "MN"  / f"MN_{region}",
            "SP":  distro / "PRODUCTS" / "SPEED_PROFILES" / f"SPEED_PROFILES_{region}",
            "POI": distro / "PRODUCTS" / "MN_PREMIUM_POI" / f"MN_PREMIUM_POI_{region}",
            "APT": distro / "PRODUCTS" / "MN_APT" / f"MN_APT_{region}",
        }
        dest = dest_map.get(prefix)
        if dest:
            try:
                count = sum(1 for _ in dest.rglob("*.tar.gz"))
                if count == 0:
                    not_ingested.append(sib)
            except Exception:
                not_ingested.append(sib)

    return len(not_ingested) == 0, not_ingested


@app.route("/api/run/ingest_dataset", methods=["POST"])
def run_ingest_dataset():
    """Ingest a single dataset, then delete source if all siblings done."""
    key     = request.json.get("key", "")
    quarter = request.json.get("quarter", "Q2_2026")
    job_id  = f"ingest_{key}_{datetime.now().strftime('%H%M%S')}"

    def _ingest():
        running_jobs[job_id] = {
            "status": "running", "output": [],
            "started": datetime.now().isoformat()}

        def log(m):
            running_jobs[job_id]["output"].append(m)

        log(f"━━━  Organising: {key}  ━━━")
        log(f"Quarter: {quarter}")
        log("")

        # Load quarterly config to get version
        config_path = QUARTERLY / f"{quarter}.yaml"
        try:
            with open(config_path) as f:
                cfg = yaml.safe_load(f)
            version = cfg.get("tomtom_version", "")
            distro  = Path(cfg.get("distro_folder", ""))
        except Exception as e:
            log(f"❌ Could not load quarterly config: {e}")
            running_jobs[job_id]["status"] = "error"
            return

        # Check disk space before starting
        try:
            import shutil
            free_gb = shutil.disk_usage("/mnt/data").free / (1024**3)
            log(f"Disk space available: {free_gb:.0f}GB")
            if free_gb < 20:
                log(f"❌ Not enough disk space ({free_gb:.0f}GB free). Free up space first.")
                running_jobs[job_id]["status"] = "error"
                return
        except:
            pass

        # Verify the download is actually complete before organising it.
        # Ingesting a partial download moves broken/incomplete data into the
        # distribution folder where clients and SFTP partners can reach it.
        force_ingest = request.json.get("force", False)
        try:
            _family = key.split("_")[0]
            _region = key.replace(f"{_family}_", "")
            _ver2 = version if _family == "MNR" else version.rsplit(".", 1)[0] + ".000"
            _pattern_fn = FOLDER_PATTERNS.get(_family)
            if _pattern_fn:
                _folder_name = _pattern_fn(_region, _ver2)
                _full_dl_path = DOWNLOADS / _folder_name
                if _full_dl_path.exists():
                    _v = verify_download_against_manifest(_full_dl_path, _folder_name)
                    if _v.get("verified") and not _v.get("complete"):
                        _pct = _v["percent_complete"]
                        _missing_gb = _v["missing_size_gb"]
                        _missing_n = _v["missing_files_count"]
                        if not force_ingest:
                            log(f"\u274c STOPPED: {key} is only {_pct}% downloaded "
                                f"({_missing_n} file(s) / {_missing_gb}GB still missing)")
                            log("")
                            log("This download has NOT finished. Organising it now would put "
                                "incomplete data into the distribution folder.")
                            log("Go back to the Pipeline page and click 'Finish downloading' "
                                "on this dataset first.")
                            running_jobs[job_id]["status"] = "error"
                            running_jobs[job_id]["blocked_incomplete"] = True
                            running_jobs[job_id]["verify"] = _v
                            return
                        else:
                            log(f"\u26a0\ufe0f  WARNING: {key} is only {_pct}% downloaded "
                                f"({_missing_gb}GB missing) - organising anyway because "
                                f"force was requested")
                            log("")
        except Exception as _ve:
            log(f"\u26a0\ufe0f  Could not verify download completeness before organising: {_ve}")

        # Regenerate .folders.env first
        gen_script = SCRIPTS / "generate_env.py"
        if gen_script.exists():
            subprocess.run(
                [sys.executable, str(gen_script), "--quarter", quarter],
                capture_output=True)
            log("✅ Folder paths updated")

        # Run ingest for this specific dataset only
        log_file = LOGS_DIR / f"ingest_{key}_{quarter}.log"
        cmd = ["bash", str(SCRIPTS / "ingest.sh"),
               "--env", str(FOLDERS_ENV),
               "--only", key,
               "--move",
               "--skip-nextcloud",
               "--log", str(log_file)]

        log(f"Starting ingest for {key}...")
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1)
        for line in proc.stdout:
            running_jobs[job_id]["output"].append(line.rstrip())
        proc.wait()

        # Verify destination has data
        prefix = key.split("_")[0]
        region = key.replace(f"{prefix}_", "")
        dest_map = {
            "MNR": distro / "MNR" / f"MNR_{region}",
            "MN":  distro / "MN"  / f"MN_{region}",
            "SP":  distro / "PRODUCTS" / "SPEED_PROFILES" / f"SPEED_PROFILES_{region}",
            "POI": distro / "PRODUCTS" / "MN_PREMIUM_POI" / f"MN_PREMIUM_POI_{region}",
            "APT": distro / "PRODUCTS" / "MN_APT" / f"MN_APT_{region}",
        }
        dest = dest_map.get(prefix)
        if dest and dest.exists():
            # MNR uses .tar.gz; MN, SP, POI, APT use .7z.001
            if prefix == "MNR":
                count = sum(1 for _ in dest.rglob("*.tar.gz"))
                ext_label = ".tar.gz"
            else:
                count = sum(1 for _ in dest.rglob("*.7z.001"))
                ext_label = ".7z.001"
            if count > 0:
                log(f"✅ Verified: {count} file(s) in {dest.name}")
            else:
                log(f"⚠️  Destination exists but no {ext_label} files found")
        else:
            log(f"⚠️  Destination folder not found: {dest}")

        # Check if all siblings are ingested — if so, delete source
        log("")
        log("Checking if source can be deleted...")
        all_done, pending = all_siblings_ingested(key, version, distro)

        if all_done:
            source = get_source_folder(key, version)
            if source and source.exists():
                log(f"✅ All related datasets ingested — deleting source:")
                log(f"   {source.name}")
                try:
                    import shutil as _shutil
                    _shutil.rmtree(str(source))
                    log(f"✅ Source deleted — disk space freed")
                    # Show new free space
                    free_gb = shutil.disk_usage("/mnt/data").free / (1024**3)
                    log(f"   Free space now: {free_gb:.0f}GB")
                except Exception as e:
                    log(f"⚠️  Could not delete source: {e}")
            else:
                log("ℹ️  Source already deleted or not found")
        else:
            log(f"ℹ️  Source kept — still needed for: {', '.join(pending)}")

        log("")
        log(f"━━━  {key} complete  ━━━")
        running_jobs[job_id]["status"] = "done" if proc.returncode == 0 else "error"

        # Notify
        send_notification_email(
            f"✅ GeoInt: {key} organised for {quarter}",
            f"<p><strong>{key}</strong> has been organised into the distribution folder.</p>"
            f"{'<p>Source download deleted — disk space freed.</p>' if all_done else ''}")

    threading.Thread(target=_ingest, daemon=True).start()
    return jsonify({"job_id": job_id})

@app.route("/api/run/ingest", methods=["POST"])
def run_ingest():
    dry_run  = request.json.get("dry_run", False)
    quarter  = request.json.get("quarter", "Q1_2026")
    job_id   = f"ingest_{datetime.now().strftime('%H%M%S')}"
    log_file = LOGS_DIR / f"ingest_{quarter}.log"

    # Auto-generate .folders.env first
    gen_script = SCRIPTS / "generate_env.py"
    if gen_script.exists():
        subprocess.run([sys.executable, str(gen_script), "--quarter", quarter],
                      capture_output=True)

    cmd = ["bash", str(SCRIPTS / "ingest.sh"),
           "--env", str(FOLDERS_ENV), "--log", str(log_file)]
    if dry_run:
        cmd.append("--dry-run")
    else:
        # Real dashboard ingests should conserve disk space. Subset datasets
        # are protected inside ingest.sh by PRESERVE_SOURCE when zone filters
        # are active, so --move is safe for the normal UI workflow.
        cmd.append("--move")

    notify = None if dry_run else (
        f"✅ GeoInt: Ingest complete for {quarter}",
        f"<p>Data for <strong>{quarter}</strong> has been organised into the distribution folder structure.</p>"
        f"<p>Next step: update Nextcloud clients so they can access their data.</p>"
        f"<p><a href='http://{load_env().get('NC_DOMAIN','localhost')}:5050'>Open Dashboard</a></p>")

    run_bg(cmd, job_id, notify_on_done=notify)
    return jsonify({"job_id": job_id})


@app.route("/api/run/provision", methods=["POST"])
def run_provision():
    dry_run = request.json.get("dry_run", False)
    job_id  = f"provision_{datetime.now().strftime('%H%M%S')}"
    quarter = request.json.get("quarter", "")
    cmd = ["python3", str(SCRIPTS / "provision_clients.py"),
           "--env", str(CONFIG_ENV),
           "--csv", str(DATA_DIR / "client_distribution.csv"),
           "--report"]
    if quarter:
        cmd += ["--quarter", quarter]
    if dry_run:
        cmd.append("--dry-run")
    notify = None if dry_run else (
        f"✅ GeoInt: Nextcloud clients updated",
        f"<p>Client accounts and folder access have been updated in Nextcloud.</p>"
        f"<p>Next step: send notification emails to clients.</p>")
    run_bg(cmd, job_id, notify_on_done=notify)
    return jsonify({"job_id": job_id})


@app.route("/api/run/notify", methods=["POST"])
def run_notify():
    quarter = request.json.get("quarter", "Q1_2026")
    dry_run = request.json.get("dry_run", True)
    job_id  = f"notify_{datetime.now().strftime('%H%M%S')}"
    cmd = ["python3", str(AGENTS / "agent_notify.py"),
           "--env", str(CONFIG_ENV), "--quarter", quarter]
    if dry_run:
        cmd.append("--dry-run")
    run_bg(cmd, job_id)
    return jsonify({"job_id": job_id})


@app.route("/api/run/scan", methods=["POST"])
def run_scan():
    job_id = f"scan_{datetime.now().strftime('%H%M%S')}"
    run_bg(["sudo", "-u", "www-data", "php", str(NC_OCC),
            "files:scan", "--all"], job_id)
    return jsonify({"job_id": job_id})


@app.route("/api/run/backup", methods=["POST"])
def run_backup():
    job_id = f"backup_{datetime.now().strftime('%H%M%S')}"

    def _backup():
        running_jobs[job_id] = {"status": "running", "output": [], "started": datetime.now().isoformat()}
        def log(m): running_jobs[job_id]["output"].append(m)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = BASE_DIR / "backups" / ts
        backup_dir.mkdir(parents=True, exist_ok=True)
        log(f"━━━  Backup starting: {ts}  ━━━")
        log(f"Saving to: {backup_dir}")
        log("")
        log("Enabling maintenance mode...")
        subprocess.run(["sudo", "-u", "www-data", "php", str(NC_OCC), "maintenance:mode", "--on"], capture_output=True)
        log("✅ Maintenance mode ON")
        log("Backing up database...")
        db_file = backup_dir / "nextcloud_db.sql.gz"
        r = subprocess.run(f"mysqldump --defaults-file=/etc/mysql/debian.cnf nextcloud | gzip > {db_file}", shell=True, capture_output=True, text=True)
        log(f"{'✅' if r.returncode==0 else '⚠️ '} Database: {db_file.name}")
        log("Backing up config...")
        nc_conf = Path("/var/www/html/nextcloud/config")
        if nc_conf.exists():
            subprocess.run(["tar", "-czf", str(backup_dir / "nc_config.tar.gz"), "-C", str(nc_conf.parent), "config"], capture_output=True)
            log("✅ Nextcloud config backed up")
        log("Backing up scripts...")
        subprocess.run(["tar", "-czf", str(backup_dir / "scripts.tar.gz"), "-C", str(BASE_DIR.parent), str(BASE_DIR.name), "--exclude=backups", "--exclude=.venv", "--exclude=__pycache__"], capture_output=True)
        log("✅ Scripts backed up")
        subprocess.run(["sudo", "-u", "www-data", "php", str(NC_OCC), "maintenance:mode", "--off"], capture_output=True)
        log("✅ Maintenance mode OFF")
        log("")
        log("━━━  Backup complete  ━━━")
        log(f"Location: {backup_dir}")
        log("Note: backs up database + config only. Distribution data is re-downloadable from TomTom.")
        running_jobs[job_id]["status"] = "done"

    threading.Thread(target=_backup, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/notify/test", methods=["POST"])
def test_notification():
    ok, msg = send_notification_email(
        "✅ GeoInt Dashboard — Test notification",
        f"<p>This is a test notification from the GeoInt Distribution Dashboard.</p>"
        f"<p>If you received this, email notifications are working correctly.</p>"
        f"<p>Sent: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>")
    return jsonify({"ok": ok, "message": msg})


# ── Notification tracking ─────────────────────────────────────
# Stored in /opt/nextcloud-setup/nextcloud-data-distribution/data/notifications.json
# Format: { "Q2_2026": { "3D_Tracking": { "sent": true, "sent_at": "...", "datasets": [...] } } }

NOTIFICATIONS_FILE = BASE_DIR / "data" / "notifications.json"


def load_notifications():
    try:
        if NOTIFICATIONS_FILE.exists():
            return json.loads(NOTIFICATIONS_FILE.read_text())
        return {}
    except:
        return {}


def save_notifications(data):
    NOTIFICATIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    NOTIFICATIONS_FILE.write_text(json.dumps(data, indent=2))


def get_client_datasets_from_csv(client_name):
    """Get datasets and groups for a client from the CSV."""
    csv_path = DATA_DIR / "client_distribution.csv"
    datasets = []
    password = ""
    try:
        import csv
        with open(csv_path) as f:
            for row in csv.DictReader(f):
                if row.get("CLIENT", "").strip() == client_name:
                    if row.get("PASSWORD", "").strip():
                        password = row["PASSWORD"].strip()
                    data_desc = row.get("DATA", "").strip()
                    if data_desc:
                        datasets.append(data_desc)
    except:
        pass
    return datasets, password


def save_password_to_csv(client_name, username, password):
    """Save or update client password in client_distribution.csv."""
    import csv, tempfile, shutil
    csv_path = DATA_DIR / "client_distribution.csv"
    if not csv_path.exists():
        return
    rows = []
    found = False
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        for row in reader:
            if row.get("CLIENT", "").strip().lower() == client_name.lower():
                row["PASSWORD"] = password
                found = True
            rows.append(row)
    if not found:
        # Add a placeholder row so password is stored
        rows.append({"CLIENT": client_name, "PASSWORD": password,
                     "DATA": "", "GROUP": "", "DirectoryPath": ""})
    tmp = csv_path.with_suffix(".tmp")
    with open(tmp, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    shutil.move(str(tmp), str(csv_path))

def build_client_email(client_name, username, password, datasets, quarter, nc_domain):
    url = f"https://{nc_domain}" if nc_domain else "https://content.geoint.africa"

    # Resolve distro folder from quarterly YAML
    distro = ""
    try:
        import yaml as _yaml
        q_file = QUARTERLY / f"{quarter}.yaml"
        if q_file.exists():
            with open(q_file) as _f:
                distro = _yaml.safe_load(_f).get("distro_folder", "")
    except:
        pass

    # Check if a path has actual data files
    def check_path(raw_path):
        try:
            p = Path(raw_path.replace("{DISTRO}", distro)) if distro else Path(raw_path)
            if not p.exists():
                return False
            for ext in ["*.tar.gz", "*.zip", "*.sql", "*.shp", "*.csv"]:
                if any(True for _ in p.rglob(ext)):
                    return True
            return sum(1 for _ in p.rglob("*") if _.is_file()) > 3
        except:
            return False

    # Build dataset rows from CSV with live status check
    items = ""
    try:
        import csv as _csv
        checked = set()
        with open(DATA_DIR / "client_distribution.csv") as _f:
            for row in _csv.DictReader(_f):
                if row.get("CLIENT", "").strip() != client_name:
                    continue
                desc = row.get("DATA", "").strip()
                path = row.get("DirectoryPath", "").strip()
                if not desc or desc in checked:
                    continue
                checked.add(desc)
                if check_path(path):
                    items += f"""<li style="padding:7px 0;border-bottom:1px solid #f2f2ef">
                        <span style="color:#0f6e56;font-weight:600">✅</span>&nbsp; {desc}</li>"""
                else:
                    items += f"""<li style="padding:7px 0;border-bottom:1px solid #f2f2ef;color:#888">
                        <span style="color:#ef9f27;font-weight:600">🔄</span>&nbsp; {desc}
                        <span style="font-size:11px;color:#aaa;display:block;margin-top:2px">
                        Being prepared — will be available when released by TomTom</span></li>"""
    except:
        items = "".join(
            f'<li style="padding:7px 0;border-bottom:1px solid #f2f2ef">✅&nbsp;{d}</li>'
            for d in datasets)

    return f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>
body{{font-family:'Segoe UI',Arial,sans-serif;color:#1a1a1a;background:#f5f5f2;margin:0;padding:20px}}
.card{{background:#fff;border-radius:12px;max-width:560px;margin:0 auto;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,0.08)}}
.hdr{{background:linear-gradient(135deg,#251F65 0%,#2EB2E6 100%);padding:32px;text-align:center;color:#fff}}
.hdr h1{{font-size:20px;margin:0;font-weight:600}}.hdr p{{opacity:.8;font-size:13px;margin:4px 0 0}}
.body{{padding:28px 32px}}.section{{margin-bottom:20px}}
.section h3{{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:#888;margin:0 0 8px;font-weight:600}}
.cred{{background:#f0f7ff;border:1px solid #b5d4f4;border-radius:8px;padding:14px 16px}}
.crow{{display:flex;gap:12px;margin-bottom:8px}}.crow:last-child{{margin-bottom:0}}
.clbl{{font-size:12px;color:#888;width:80px;flex-shrink:0}}
.cval{{font-family:monospace;font-size:14px;color:#185fa5;font-weight:600;word-break:break-all}}
.btn{{display:block;background:#185fa5;color:#fff;text-decoration:none;text-align:center;padding:12px;border-radius:8px;font-size:14px;font-weight:600;margin-top:16px}}
ul{{list-style:none;padding:0;margin:0}}
.footer{{padding:16px 32px;background:#f8f8f6;text-align:center;font-size:11px;color:#aaa}}
</style></head><body>
<div class="card">
<div class="hdr"><h1>GeoInt Data Delivery — {quarter}</h1><p>Your quarterly data is ready to access</p></div>
<div class="body">
<p style="margin-bottom:16px">Dear <strong>{client_name}</strong>,<br><br>
Your <strong>{quarter}</strong> geospatial data is now available in your dedicated Nextcloud instance.</p>
<div class="section"><h3>Login Details</h3>
<div class="cred">
<div class="crow"><span class="clbl">URL</span><span class="cval"><a href="{url}" style="color:#185fa5">{url}</a></span></div>
<div class="crow"><span class="clbl">Username</span><span class="cval">{username}</span></div>
<div class="crow"><span class="clbl">Password</span><span class="cval">{password}</span></div>
</div>
<a href="{url}" class="btn">Log in to Nextcloud →</a></div>
<div class="section"><h3>Your Data This Quarter</h3><ul>{items}</ul></div>
</div>
<div class="footer">GeoInt Africa · Shaping the Future of Where · support@geoint.africa</div>
</div></body></html>"""




@app.route("/api/notifications/<quarter>")
def get_notifications(quarter):
    """Return notification status for all clients for a quarter."""
    notifications = load_notifications()
    quarter_notifs = notifications.get(quarter, {})

    # Load client list from quarterly YAML
    config_path = QUARTERLY / f"{quarter}.yaml"
    clients = {}
    if config_path.exists():
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        for client_name, client_data in cfg.get("clients", {}).items():
            username = client_data.get("username", client_name.lower()) if client_data else client_name.lower()
            datasets, password = get_client_datasets_from_csv(client_name)
            notif = quarter_notifs.get(client_name, {})
            clients[client_name] = {
                "client":    client_name,
                "username":  username,
                "datasets":  datasets,
                "password":  password,
                "sent":      notif.get("sent", False),
                "sent_at":   notif.get("sent_at", ""),
                "email_id":  notif.get("email_id", ""),
            }

    return jsonify(clients)


@app.route("/api/notifications/send", methods=["POST"])
def send_client_notification():
    """Send notification email for a specific client."""
    client_name = request.json.get("client")
    quarter     = request.json.get("quarter")
    force       = request.json.get("force", False)  # re-send even if already sent

    if not client_name or not quarter:
        return jsonify({"error": "client and quarter required"}), 400

    notifications = load_notifications()
    if quarter not in notifications:
        notifications[quarter] = {}

    # Check if already sent
    if notifications[quarter].get(client_name, {}).get("sent") and not force:
        return jsonify({
            "ok": False,
            "error": "already_sent",
            "message": f"Email already sent to support for {client_name} on "
                       f"{notifications[quarter][client_name].get('sent_at', '?')}. "
                       f"Use force=true to re-send."
        })

    # Load client info
    config_path = QUARTERLY / f"{quarter}.yaml"
    if not config_path.exists():
        return jsonify({"error": "Quarter config not found"}), 404

    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    client_cfg = cfg.get("clients", {}).get(client_name, {})
    username   = client_cfg.get("username", client_name.lower()) if client_cfg else client_name.lower()
    datasets, password = get_client_datasets_from_csv(client_name)

    env        = load_env()
    nc_domain  = env.get("NC_DOMAIN", "content.geoint.africa")

    # Build and send email
    email_html = build_client_email(
        client_name, username, password, datasets, quarter, nc_domain)

    subject = f"GeoInt {quarter} Data Ready — {client_name}"
    ok, msg = send_notification_email(subject, email_html)

    if ok:
        email_id = str(uuid.uuid4())[:8]
        notifications[quarter][client_name] = {
            "sent":     True,
            "sent_at":  datetime.now().strftime("%Y-%m-%d %H:%M"),
            "email_id": email_id,
            "datasets": datasets,
            "username": username,
        }
        save_notifications(notifications)
        return jsonify({
            "ok": True,
            "message": f"Email sent to support@geoint.africa for {client_name}",
            "email_id": email_id,
        })
    else:
        return jsonify({"ok": False, "error": msg}), 500


@app.route("/api/notifications/send_all", methods=["POST"])
def send_all_notifications():
    """Send notification emails for all clients not yet notified."""
    quarter   = request.json.get("quarter")
    force     = request.json.get("force", False)
    job_id    = f"notify_all_{datetime.now().strftime('%H%M%S')}"

    def _send_all():
        running_jobs[job_id] = {
            "status": "running", "output": [],
            "started": datetime.now().isoformat()}

        def log(m):
            running_jobs[job_id]["output"].append(m)

        config_path = QUARTERLY / f"{quarter}.yaml"
        if not config_path.exists():
            log("❌ Quarter config not found")
            running_jobs[job_id]["status"] = "error"
            return

        with open(config_path) as f:
            cfg = yaml.safe_load(f)

        clients    = cfg.get("clients", {})
        total      = len(clients)
        sent_count = 0
        skip_count = 0
        fail_count = 0

        log(f"━━━  Sending notifications for {quarter}  ━━━")
        log(f"Clients: {total}")
        log("")

        notifications = load_notifications()
        if quarter not in notifications:
            notifications[quarter] = {}

        env       = load_env()
        nc_domain = env.get("NC_DOMAIN", "content.geoint.africa")

        for client_name, client_data in clients.items():
            already = notifications[quarter].get(client_name, {}).get("sent", False)
            if already and not force:
                log(f"⏭️  {client_name} — already notified, skipping")
                skip_count += 1
                continue

            username = client_data.get("username", client_name.lower()) if client_data else client_name.lower()
            datasets, password = get_client_datasets_from_csv(client_name)

            email_html = build_client_email(
                client_name, username, password, datasets, quarter, nc_domain)
            subject = f"GeoInt {quarter} Data Ready — {client_name}"

            ok, msg = send_notification_email(subject, email_html)
            if ok:
                email_id = str(uuid.uuid4())[:8]
                notifications[quarter][client_name] = {
                    "sent":     True,
                    "sent_at":  datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "email_id": email_id,
                    "datasets": datasets,
                    "username": username,
                }
                save_notifications(notifications)
                log(f"✅ {client_name} — sent (ref: {email_id})")
                sent_count += 1
            else:
                log(f"❌ {client_name} — failed: {msg}")
                fail_count += 1

        log("")
        log(f"━━━  Complete  ━━━")
        log(f"Sent:    {sent_count}")
        log(f"Skipped: {skip_count} (already notified)")
        log(f"Failed:  {fail_count}")
        running_jobs[job_id]["status"] = "done" if fail_count == 0 else "error"

    threading.Thread(target=_send_all, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/notifications/preview/<quarter>/<client>")
def preview_notification(quarter, client):
    """Return a preview of the email that would be sent."""
    config_path = QUARTERLY / f"{quarter}.yaml"
    if not config_path.exists():
        return jsonify({"error": "not found"}), 404

    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    client_cfg = cfg.get("clients", {}).get(client, {})
    username   = client_cfg.get("username", client.lower()) if client_cfg else client.lower()
    datasets, password = get_client_datasets_from_csv(client)
    env        = load_env()
    nc_domain  = env.get("NC_DOMAIN", "content.geoint.africa")

    html = build_client_email(client, username, password, datasets, quarter, nc_domain)
    return jsonify({"html": html, "subject": f"GeoInt {quarter} Data Ready — {client}"})



@app.route("/api/notifications/jira/<quarter>/<client>")
def jira_message(quarter, client):
    """Return a formatted Jira comment message for a client."""
    config_path = QUARTERLY / f"{quarter}.yaml"
    if not config_path.exists():
        return jsonify({"error": "not found"}), 404

    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    client_cfg = cfg.get("clients", {}).get(client, {})
    username   = client_cfg.get("username", client.lower()) if client_cfg else client.lower()
    datasets, password = get_client_datasets_from_csv(client)
    env        = load_env()
    nc_domain  = env.get("NC_DOMAIN", "content.geoint.africa")
    url        = f"https://{nc_domain}"

    # Check which datasets are available
    distro = ""
    try:
        distro = cfg.get("distro_folder", "")
    except:
        pass

    def check_path(raw_path):
        try:
            p = Path(raw_path.replace("{DISTRO}", distro)) if distro else Path(raw_path)
            if not p.exists():
                return False
            for ext in ["*.tar.gz", "*.zip", "*.sql", "*.shp", "*.csv"]:
                if any(True for _ in p.rglob(ext)):
                    return True
            return sum(1 for _ in p.rglob("*") if _.is_file()) > 3
        except:
            return False

    # Build dataset lines
    dataset_lines = []
    try:
        import csv as _csv
        checked = set()
        with open(DATA_DIR / "client_distribution.csv") as _f:
            for row in _csv.DictReader(_f):
                if row.get("CLIENT", "").strip() != client:
                    continue
                desc = row.get("DATA", "").strip()
                path = row.get("DirectoryPath", "").strip()
                if not desc or desc in checked:
                    continue
                checked.add(desc)
                if check_path(path):
                    dataset_lines.append(f"(/) {desc}")
                else:
                    dataset_lines.append(f"(~) {desc} -- being prepared, available when released by TomTom")
    except:
        dataset_lines = [f"(/) {d}" for d in datasets]

    ref = f"GEO-{quarter}-{username}-{datetime.now().strftime('%Y-%m-%d')}"

    # Clean plain text for Jira comments
    datasets_text = chr(10).join(
        f"  • {line.replace('(/) ', '').replace('(~) ', '').replace(' -- being prepared, available when released by TomTom', ' — coming soon')}"
        for line in dataset_lines
    )

    msg = f"""GeoInt {quarter} — Data Delivery

Client:   {client}
Username: {username}
Password: {password}
URL:      {url}

Data this quarter:
{datasets_text}
"""

    return jsonify({"message": msg.strip(), "client": client})




@app.route("/api/cleanup/quarters")
def get_old_quarters():
    """
    List all DISTRIBUTION_Q*_* folders under /mnt/data with size and
    last-modified date. Flags the currently active quarter (the one
    DISTRIBUTION_CURRENT points to) as protected - it cannot be deleted
    from this tool.
    """
    current_target = None
    symlink = DISTRO_BASE / "DISTRIBUTION_CURRENT"
    if symlink.is_symlink():
        try:
            current_target = symlink.resolve().name
        except Exception:
            current_target = None

    quarters = []
    for d in sorted(DISTRO_BASE.glob("DISTRIBUTION_Q*_*")):
        if not d.is_dir():
            continue
        try:
            r = subprocess.run(["du", "-sh", str(d)],
                               capture_output=True, text=True, timeout=60)
            size = r.stdout.split()[0] if r.stdout else "?"
        except Exception:
            size = "?"
        try:
            mtime = datetime.fromtimestamp(d.stat().st_mtime).strftime("%Y-%m-%d")
        except Exception:
            mtime = "?"
        is_current = (d.name == current_target)
        quarters.append({
            "name": d.name,
            "path": str(d),
            "size": size,
            "last_modified": mtime,
            "is_current": is_current,
            "protected": is_current,
        })
    return jsonify({"quarters": quarters, "current": current_target})


@app.route("/api/cleanup/quarters/delete", methods=["POST"])
def delete_old_quarter():
    """
    Delete an old DISTRIBUTION_Q*_* folder entirely. Refuses to delete the
    quarter that DISTRIBUTION_CURRENT points to, and refuses any path that
    is not directly under /mnt/data and matching the expected naming
    pattern, as a safety guard against deleting the wrong thing.
    """
    folder_name = request.json.get("folder_name", "")
    confirm_name = request.json.get("confirm_name", "")

    if not folder_name or not folder_name.startswith("DISTRIBUTION_Q"):
        return jsonify({"error": "Invalid folder name"}), 400
    if folder_name != confirm_name:
        return jsonify({"error": "Confirmation text does not match folder name"}), 400

    target = DISTRO_BASE / folder_name
    if not target.exists() or not target.is_dir():
        return jsonify({"error": "Folder not found"}), 404
    if str(target.resolve()).strip("/") == "" or target.resolve() == DISTRO_BASE.resolve():
        return jsonify({"error": "Refusing to delete root data folder"}), 400

    symlink = DISTRO_BASE / "DISTRIBUTION_CURRENT"
    if symlink.is_symlink():
        try:
            if symlink.resolve() == target.resolve():
                return jsonify({"error": "Cannot delete the currently active quarter"}), 400
        except Exception:
            pass

    job_id = f"delquarter_{datetime.now().strftime('%H%M%S')}"

    def _delete():
        running_jobs[job_id] = {"status": "running", "output": [], "started": datetime.now().isoformat()}
        def log(m): running_jobs[job_id]["output"].append(m)
        log(f"\u2501\u2501\u2501  Deleting {folder_name}  \u2501\u2501\u2501")
        try:
            r = subprocess.run(["du", "-sh", str(target)], capture_output=True, text=True, timeout=60)
            size = r.stdout.split()[0] if r.stdout else "?"
            log(f"Size to be freed: {size}")
        except Exception:
            pass
        try:
            import shutil as _shutil
            _shutil.rmtree(str(target))
            log(f"\u2705 Deleted: {target}")
            try:
                r = subprocess.run(["df", "-h", "/mnt/data"], capture_output=True, text=True)
                parts = r.stdout.strip().split("\n")[1].split()
                log(f"Disk space now: {parts[3]} free ({parts[4]} used)")
            except Exception:
                pass
            running_jobs[job_id]["status"] = "done"
        except Exception as e:
            log(f"\u274c Failed to delete: {e}")
            running_jobs[job_id]["status"] = "error"

    threading.Thread(target=_delete, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/cleanup/downloads/<quarter>")
def get_cleanup_candidates(quarter):
    """
    Return download folders that are safe to delete —
    i.e. their data has been fully ingested into the distribution folder.
    """
    config_path = QUARTERLY / f"{quarter}.yaml"
    if not config_path.exists():
        return jsonify({"error": "Quarter config not found"}), 404

    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    version = cfg.get("tomtom_version", "")
    # Use base version (e.g. "2026.06") to match all product variants
    version_base = ".".join(version.split(".")[:2]) if version else ""
    distro  = Path(cfg.get("distro_folder", ""))

    candidates = []
    safe_to_delete = []

    # Check each folder in downloads
    if DOWNLOADS.exists():
        for folder in DOWNLOADS.iterdir():
            if not folder.is_dir():
                continue
            try:
                size_r = subprocess.run(
                    ["du", "-sh", str(folder)],
                    capture_output=True, text=True, timeout=30)
                size = size_r.stdout.split()[0] if size_r.stdout else "?"
            except:
                size = "?"

            # Check if this folder has been ingested
            # Match folder name to a known product family
            fname = folder.name
            ingested = False
            dest_hint = ""

            if version_base and version_base in fname and "MultiNet-R_MEA" in fname in fname:
                dest = distro / "MNR" / "MNR_MEA"
                count = sum(1 for _ in dest.rglob("*.tar.gz")) if dest.exists() else 0
                ingested = count > 0
                dest_hint = f"MNR_MEA ({count} files ingested)"
            elif version_base and version_base in fname and "MultiNet-R_EUR" in fname in fname:
                dest = distro / "MNR" / "MNR_EUR"
                count = sum(1 for _ in dest.rglob("*.tar.gz")) if dest.exists() else 0
                ingested = count > 0
                dest_hint = f"MNR_EUR ({count} files ingested)"
            elif version_base and version_base in fname and "MultiNet_MEA" in fname in fname:
                dest = distro / "MN" / "MN_MEA"
                count = sum(1 for _ in dest.rglob("*.tar.gz")) if dest.exists() else 0
                ingested = count > 0
                dest_hint = f"MN_MEA ({count} files ingested)"
            elif version_base and version_base in fname and "MultiNet_EUR" in fname in fname:
                dest = distro / "MN" / "MN_EUR"
                count = sum(1 for _ in dest.rglob("*.tar.gz")) if dest.exists() else 0
                ingested = count > 0
                dest_hint = f"MN_EUR ({count} files ingested)"
            elif version_base and version_base in fname and ("MultiNet-R_SOUTHERN_AFRICA" in fname or "MultiNet-R_ZAF" in fname):
                region = "SOUTHERN_AFRICA" if "SOUTHERN_AFRICA" in fname else "ZAF"
                dest = distro / "MNR" / f"MNR_{region}"
                count = sum(1 for _ in dest.rglob("*.tar.gz")) if dest.exists() else 0
                ingested = count > 0
                dest_hint = f"MNR_{region} ({count} files ingested)"
            elif version_base and version_base in fname and "MultiNet-POI" in fname:
                dest = distro / "PRODUCTS" / "MN_PREMIUM_POI" / "MN_PREMIUM_POI_MEA"
                count = sum(1 for _ in dest.rglob("*")) if dest.exists() else 0
                ingested = dest.exists() and count > 0
                dest_hint = f"MN_PREMIUM_POI_MEA ({count} files ingested)"
            elif version_base and version_base in fname and "MultiNet-SpeedProfile_MEA" in fname:
                dest = distro / "PRODUCTS" / "SPEED_PROFILES" / "SPEED_PROFILES_MEA"
                count = sum(1 for _ in dest.rglob("*")) if dest.exists() else 0
                ingested = dest.exists() and count > 0
                dest_hint = f"SPEED_PROFILES_MEA ({count} files ingested)"
            elif version_base and version_base in fname and "MultiNet-SpeedProfile_EUR" in fname:
                dest = distro / "PRODUCTS" / "SPEED_PROFILES" / "SPEED_PROFILES_EUR"
                count = sum(1 for _ in dest.rglob("*")) if dest.exists() else 0
                ingested = dest.exists() and count > 0
                dest_hint = f"SPEED_PROFILES_EUR ({count} files ingested)"
            elif version_base and version_base in fname and "MultiNet-SpeedProfile_ZAF" in fname:
                dest = distro / "PRODUCTS" / "SPEED_PROFILES" / "SPEED_PROFILES_ZAF"
                count = sum(1 for _ in dest.rglob("*")) if dest.exists() else 0
                ingested = dest.exists() and count > 0
                dest_hint = f"SPEED_PROFILES_ZAF ({count} files ingested)"
            elif fname in ["clients", "downloads"]:
                ingested = True  # Legacy folders — always safe to delete
                dest_hint = "Legacy folder — no longer needed"
            else:
                dest_hint = "Unknown — manual check recommended"

            entry = {
                "name":       fname,
                "path":       str(folder),
                "size":       size,
                "ingested":   ingested,
                "dest_hint":  dest_hint,
                "safe":       ingested,
            }
            candidates.append(entry)
            if ingested:
                safe_to_delete.append(entry)

    return jsonify({
        "candidates":    candidates,
        "safe_to_delete": safe_to_delete,
        "total_folders": len(candidates),
        "safe_count":    len(safe_to_delete),
    })


@app.route("/api/cleanup/downloads", methods=["POST"])
def run_cleanup():
    """Delete specified download folders after verifying they are ingested."""
    folders  = request.json.get("folders", [])
    quarter  = request.json.get("quarter", "")
    job_id   = f"cleanup_{datetime.now().strftime('%H%M%S')}"

    def _cleanup():
        running_jobs[job_id] = {
            "status": "running", "output": [],
            "started": datetime.now().isoformat()}

        def log(m):
            running_jobs[job_id]["output"].append(m)

        log(f"━━━  Download Cleanup  ━━━")
        log(f"Folders to delete: {len(folders)}")
        log("")

        freed = 0
        for folder_path in folders:
            p = Path(folder_path)
            if not p.exists():
                log(f"⚠️  Already gone: {p.name}")
                continue

            # Final safety check — only delete from downloads folder
            if str(DOWNLOADS) not in str(p):
                log(f"❌ Skipped (not in downloads folder): {p.name}")
                continue

            try:
                # Get size before deleting
                r = subprocess.run(
                    ["du", "-sh", str(p)],
                    capture_output=True, text=True, timeout=30)
                size = r.stdout.split()[0] if r.stdout else "?"

                import shutil as _shutil
                _shutil.rmtree(str(p))
                log(f"✅ Deleted: {p.name} ({size})")
            except Exception as e:
                log(f"❌ Failed to delete {p.name}: {e}")

        # Show new free space
        try:
            r = subprocess.run(
                ["df", "-h", "/mnt/data"],
                capture_output=True, text=True)
            parts = r.stdout.strip().split("\n")[1].split()
            log("")
            log(f"Disk space now: {parts[3]} free ({parts[4]} used)")
        except:
            pass

        log("")
        log("━━━  Cleanup complete  ━━━")
        running_jobs[job_id]["status"] = "done"

    threading.Thread(target=_cleanup, daemon=True).start()
    return jsonify({"job_id": job_id})




# ══════════════════════════════════════════════════════════════════════════════
#  CLIENT FILE UPLOADS
# ══════════════════════════════════════════════════════════════════════════════

UPLOADS_ROOT = Path("/mnt/data/UPLOADS")
NC_PATH_UPLOADS = "/var/www/html/nextcloud"

def ensure_upload_folder(username, folder_name='Custom Files', folder_slug=None):
    """Create upload folder and Nextcloud mount for a client if not exists."""
    import subprocess, json
    slug = folder_slug or 'custom_files'
    folder = UPLOADS_ROOT / username / slug
    folder.mkdir(parents=True, exist_ok=True)

    # Check if mount already exists for this user
    result = subprocess.run(
        ["sudo", "-u", "www-data", "php", f"{NC_PATH_UPLOADS}/occ",
         "files_external:list", "--output=json"],
        capture_output=True, text=True
    )
    try:
        mounts = json.loads(result.stdout) if result.stdout.strip() else []
    except Exception:
        mounts = []

    mount_name = f"/{folder_name.replace(' ', '_')}_{username}"
    already_mounted = any(
        m.get("mount_point") == mount_name
        for m in mounts
    )

    if not already_mounted:
        # Create the mount
        r = subprocess.run(
            ["sudo", "-u", "www-data", "php", f"{NC_PATH_UPLOADS}/occ",
             "files_external:create", mount_name, "local", "null::null",
             "-c", f"datadir={folder}", "--output=json"],
            capture_output=True, text=True
        )
        try:
            mount_id = json.loads(r.stdout.strip()) if r.stdout.strip() else None
        except Exception:
            mount_id = None

        if mount_id:
            # Remove from All, assign to user only, set read-only
            subprocess.run(
                ["sudo", "-u", "www-data", "php", f"{NC_PATH_UPLOADS}/occ",
                 "files_external:applicable", "--remove-all", str(mount_id)],
                capture_output=True
            )
            subprocess.run(
                ["sudo", "-u", "www-data", "php", f"{NC_PATH_UPLOADS}/occ",
                 "files_external:applicable", "--add-user", username, str(mount_id)],
                capture_output=True
            )
            subprocess.run(
                ["sudo", "-u", "www-data", "php", f"{NC_PATH_UPLOADS}/occ",
                 "files_external:option", str(mount_id), "read_only", "1"],
                capture_output=True
            )
    return str(folder)


@app.route("/api/uploads/clients")
def list_upload_clients():
    """List all clients with their upload folder status."""
    import subprocess, json
    result = subprocess.run(
        ["sudo", "-u", "www-data", "php", f"{NC_PATH_UPLOADS}/occ",
         "user:list", "--output=json"],
        capture_output=True, text=True
    )
    try:
        users = json.loads(result.stdout) if result.stdout.strip() else {}
    except Exception:
        users = {}

    clients = []
    for username, display in users.items():
        if username in ("admin",):
            continue
        folder = UPLOADS_ROOT / username
        files = []
        if folder.exists():
            for f in sorted(folder.iterdir()):
                if f.is_file():
                    files.append({
                        "name": f.name,
                        "size": f.stat().st_size,
                        "modified": f.stat().st_mtime,
                    })
        clients.append({
            "username": username,
            "display": display,
            "folder": str(folder),
            "file_count": len(files),
            "files": files,
            "has_mount": folder.exists(),
        })
    return jsonify(sorted(clients, key=lambda x: x["display"]))


@app.route("/api/uploads/upload/<username>", methods=["POST"])
def upload_file(username):
    """Upload one or more files for a client."""
    import subprocess
    if "files" not in request.files:
        return jsonify({"error": "No files provided"}), 400

    folder = ensure_upload_folder(username)
    saved = []
    errors = []

    for f in request.files.getlist("files"):
        if not f.filename:
            continue
        dest = Path(folder) / f.filename
        try:
            f.save(str(dest))
            # Fix ownership so www-data can read
            subprocess.run(["chown", "www-data:www-data", str(dest)], capture_output=True)
            saved.append(f.filename)
        except Exception as e:
            errors.append(f"{f.filename}: {str(e)}")

    # Trigger Nextcloud scan for this user
    subprocess.run(
        ["sudo", "-u", "www-data", "php", f"{NC_PATH_UPLOADS}/occ",
         "files:scan", username],
        capture_output=True
    )

    return jsonify({"saved": saved, "errors": errors})


@app.route("/api/uploads/delete/<username>/<filename>", methods=["DELETE"])
def delete_upload(username, filename):
    """Delete an uploaded file for a client."""
    import subprocess, re
    # Sanitise filename — no path traversal
    if "/" in filename or "\\" in filename or ".." in filename:
        return jsonify({"error": "Invalid filename"}), 400
    target = UPLOADS_ROOT / username / filename
    if not target.exists():
        return jsonify({"error": "File not found"}), 404
    target.unlink()
    subprocess.run(
        ["sudo", "-u", "www-data", "php", f"{NC_PATH_UPLOADS}/occ",
         "files:scan", username],
        capture_output=True
    )
    return jsonify({"ok": True})


@app.route("/api/uploads/rename/<username>", methods=["POST"])
def rename_upload(username):
    import subprocess
    data = request.get_json(silent=True) or {}
    old_name = data.get("old_name", "")
    new_name = data.get("new_name", "")
    if not old_name or not new_name:
        return jsonify({"error": "old_name and new_name required"}), 400
    if "/" in new_name or ".." in new_name or "/" in old_name or ".." in old_name:
        return jsonify({"error": "Invalid filename"}), 400
    src = UPLOADS_ROOT / username / old_name
    dst = UPLOADS_ROOT / username / new_name
    if not src.exists():
        return jsonify({"error": "File not found"}), 404
    if dst.exists():
        return jsonify({"error": "A file with that name already exists"}), 409
    src.rename(dst)
    subprocess.run(["sudo", "-u", "www-data", "php", "/var/www/html/nextcloud/occ",
        "files:scan", username], capture_output=True)
    return jsonify({"ok": True})


@app.route("/api/tools/mapflow-cache")
def mapflow_cache_status():
    import subprocess
    cache_dirs = [
        Path("/mnt/data/downloads/clients"),
        Path("/mnt/data/downloads/downloads/clients"),
    ]
    folders = []
    for d in cache_dirs:
        if d.exists():
            folders.append(str(d))
    total = 0
    for f in folders:
        r = subprocess.run(["du", "-sb", f], capture_output=True, text=True)
        try:
            total += int(r.stdout.split()[0])
        except:
            pass
    def fmt(b):
        if b > 1073741824: return f"{b/1073741824:.1f}G"
        if b > 1048576: return f"{b/1048576:.1f}M"
        return f"{b/1024:.1f}K"
    return jsonify({"folder_count": len(folders), "total_size": fmt(total), "folders": folders})

@app.route("/api/tools/mapflow-cache-clear", methods=["POST"])
def mapflow_cache_clear():
    import uuid as _uuid
    job_id = str(_uuid.uuid4())[:8]
    running_jobs[job_id] = {"status": "running", "output": []}
    def _clear():
        def log(m): running_jobs[job_id]["output"].append(m)
        log("━━━  Clearing MapFlow cache  ━━━")
        cache_dirs = [
            Path("/mnt/data/downloads/clients"),
            Path("/mnt/data/downloads/downloads/clients"),
        ]
        import shutil
        freed = 0
        for d in cache_dirs:
            if d.exists():
                r = subprocess.run(["du", "-sb", str(d)], capture_output=True, text=True)
                try: freed += int(r.stdout.split()[0])
                except: pass
                shutil.rmtree(str(d))
                log(f"✅ Deleted: {d}")
            else:
                log(f"⏭️  Not found: {d}")
        def fmt(b):
            if b > 1073741824: return f"{b/1073741824:.1f}G"
            if b > 1048576: return f"{b/1048576:.1f}M"
            return f"{b/1024:.1f}K"
        log(f"")
        log(f"✅ Done — freed {fmt(freed)}")
        running_jobs[job_id]["status"] = "done"
    threading.Thread(target=_clear, daemon=True).start()
    return jsonify({"job_id": job_id})


# ══════════════════════════════════════════════════════════════════════════════
#  NEW QUARTER WIZARD
# ══════════════════════════════════════════════════════════════════════════════

QUARTER_MONTHS = {"Q1": "03", "Q2": "06", "Q3": "09", "Q4": "12"}

def next_quarter(current):
    """Given Q2_2026 return Q3_2026, Q4_2026 → Q1_2027 etc."""
    q, y = current.split("_")
    qnum = int(q[1])
    year = int(y)
    if qnum == 4:
        return f"Q1_{year + 1}"
    return f"Q{qnum + 1}_{year}"

def quarter_version(quarter):
    """Q3_2026 → 2026.09.003"""
    q, y = quarter.split("_")
    month = QUARTER_MONTHS[q]
    return f"{y}.{month}.003"

@app.route("/api/wizard/next-quarter-preview")
def next_quarter_preview():
    """Preview what the next quarter wizard will do."""
    # Find current active quarter
    current = None
    symlink = Path("/mnt/data/DISTRIBUTION_CURRENT")
    if symlink.is_symlink():
        target = symlink.resolve().name
        # Extract quarter from folder name e.g. DISTRIBUTION_Q2_2026
        parts = target.replace("DISTRIBUTION_", "")
        if "_" in parts:
            current = parts  # e.g. Q2_2026

    if not current:
        # Fall back to latest YAML
        yamls = sorted(QUARTERLY.glob("Q*.yaml"), reverse=True)
        if yamls:
            current = yamls[0].stem
        else:
            return jsonify({"error": "Cannot determine current quarter"}), 400

    nq = next_quarter(current)
    version = quarter_version(nq)
    distro = f"/mnt/data/DISTRIBUTION_{nq}"
    yaml_path = QUARTERLY / f"{nq}.yaml"

    return jsonify({
        "current_quarter": current,
        "next_quarter": nq,
        "version": version,
        "distro_folder": distro,
        "yaml_exists": yaml_path.exists(),
        "distro_exists": Path(distro).exists(),
        "steps": [
            f"Create YAML config: quarterly/{nq}.yaml",
            f"Create distribution folder: {distro}",
            f"Run folder structure creation (create_structure)",
            f"Update DISTRIBUTION_CURRENT symlink → {distro}",
            f"Reload bind mounts (mount -a)",
            f"Trigger Nextcloud file scan",
        ]
    })

@app.route("/api/wizard/create-next-quarter", methods=["POST"])
def create_next_quarter():
    import uuid as _uuid, shutil, subprocess
    job_id = str(_uuid.uuid4())[:8]
    running_jobs[job_id] = {"status": "running", "output": []}

    data = request.get_json(silent=True) or {}
    nq = data.get("quarter")
    version = data.get("version")
    if not nq or not version:
        return jsonify({"error": "quarter and version required"}), 400

    def _run():
        def log(m): running_jobs[job_id]["output"].append(m)

        log(f"━━━  Creating {nq} Quarter  ━━━")
        log("")

        # 1. Find previous quarter YAML to copy from
        prev_q = None
        q, y = nq.split("_")
        qnum = int(q[1])
        year = int(y)
        if qnum == 1:
            prev_q = f"Q4_{year - 1}"
        else:
            prev_q = f"Q{qnum - 1}_{year}"

        prev_yaml = QUARTERLY / f"{prev_q}.yaml"
        new_yaml  = QUARTERLY / f"{nq}.yaml"

        if new_yaml.exists():
            log(f"⚠️  {nq}.yaml already exists — skipping YAML creation")
        else:
            if not prev_yaml.exists():
                log(f"❌ Previous YAML not found: {prev_yaml}")
                running_jobs[job_id]["status"] = "error"
                return
            # Copy and update
            with open(prev_yaml) as f:
                yaml_content = f.read()
            yaml_content = yaml_content.replace(
                f"quarter: {prev_q}", f"quarter: {nq}"
            ).replace(
                f"distro_folder: /mnt/data/DISTRIBUTION_{prev_q}",
                f"distro_folder: /mnt/data/DISTRIBUTION_{nq}"
            )
            # Update tomtom_version
            import re
            yaml_content = re.sub(
                r"tomtom_version:.*",
                f"tomtom_version: {version}",
                yaml_content
            )
            with open(new_yaml, "w") as f:
                f.write(yaml_content)
            log(f"✅ Created {nq}.yaml (copied from {prev_q})")
            log(f"   Version: {version}")

        # 2. Create distribution folder
        distro = Path(f"/mnt/data/DISTRIBUTION_{nq}")
        if distro.exists():
            log(f"⚠️  {distro} already exists — skipping")
        else:
            distro.mkdir(parents=True, exist_ok=True)
            log(f"✅ Created {distro}")

        # 3. Run create_structure
        log("")
        log("── Creating folder structure ──")
        env = load_env()
        script = BASE_DIR / "scripts" / "ingest.sh"
        r = subprocess.run(
            ["sudo", "-u", "www-data", "php",
             f"{NC_PATH}/occ", "files:scan", "--all"],
            capture_output=True, text=True, timeout=30
        )
        # Use the existing create_structure logic
        cfg_path = QUARTERLY / f"{nq}.yaml"
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f)
        _create_structure_for_quarter(nq, cfg, log)

        # 4. Update symlink
        log("")
        log("── Updating DISTRIBUTION_CURRENT symlink ──")
        symlink = Path("/mnt/data/DISTRIBUTION_CURRENT")
        try:
            if symlink.is_symlink() or symlink.exists():
                symlink.unlink()
            symlink.symlink_to(distro)
            log(f"✅ DISTRIBUTION_CURRENT → {distro}")
        except Exception as e:
            log(f"❌ Symlink update failed: {e}")

        # 5. Reload bind mounts
        log("")
        log("── Reloading bind mounts ──")
        r = subprocess.run(["mount", "-a"], capture_output=True, text=True)
        if r.returncode == 0:
            log("✅ Bind mounts reloaded")
        else:
            log(f"⚠️  mount -a: {r.stderr.strip()}")

        # 6. NC scan
        log("")
        log("── Triggering Nextcloud file scan ──")
        r = subprocess.run(
            ["sudo", "-u", "www-data", "php", f"{NC_PATH}/occ", "files:scan", "--all"],
            capture_output=True, text=True, timeout=120
        )
        if r.returncode == 0:
            log("✅ Nextcloud scan complete")
        else:
            log(f"⚠️  NC scan: {r.stderr.strip()}")

        log("")
        log(f"✅  {nq} is ready — go to Pipeline to start downloads")
        running_jobs[job_id]["status"] = "done"

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"job_id": job_id})

def _create_structure_for_quarter(quarter, cfg, log):
    """Create the distribution folder structure for a quarter."""
    import subprocess
    distro = Path(cfg.get("distro_folder", ""))
    if not distro.exists():
        distro.mkdir(parents=True, exist_ok=True)
        log(f"✅ Created {distro}")
    # Run the existing create_structure pipeline action
    env_path = BASE_DIR / "config" / ".env"
    folders_env = BASE_DIR / ".folders.env"
    script = BASE_DIR / "scripts" / "build_distribution.sh"
    if script.exists():
        r = subprocess.run(
            ["bash", str(script), quarter],
            capture_output=True, text=True,
            env={**os.environ, "QUARTER": quarter},
            timeout=120
        )
        if r.returncode == 0:
            log("✅ Folder structure created")
        else:
            log(f"⚠️  Structure script: {r.stderr.strip()[:200]}")
    else:
        log("⚠️  build_distribution.sh not found — folder created, structure will be built in pipeline Step 1")


# ══════════════════════════════════════════════════════════════════════════════
#  SFTP MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

SFTP_CLIENTS = {
    "tracker": {
        "label": "Tracker",
        "host": "sftp.tracker.co.za",
        "port": 22,
        "user": "mapit",
        "remote_base": "/home/mapit",
        "quarters": "all",
        "datasets": {
            "MNR_MEA":              "/mnt/data/DISTRIBUTION_CURRENT/MNR/MNR_MEA",
            "MNR_DOCUMENTATION_MEA":"/mnt/data/DISTRIBUTION_CURRENT/MNR/MNR_DOCUMENTATION/MNR_DOCUMENTATION_MEA",
            "MN_MEA":               "/mnt/data/DISTRIBUTION_CURRENT/MN/MN_MEA",
            "POI_MEA":              "/mnt/data/DISTRIBUTION_CURRENT/PRODUCTS/MN_PREMIUM_POI/MN_PREMIUM_POI_MEA",
            "SP_ZAF":               "/mnt/data/DISTRIBUTION_CURRENT/PRODUCTS/SPEED_PROFILES/SPEED_PROFILES_ZAF",
        },
    },
    "autotrak_sftp": {
        "label": "Autotrak (SFTP)",
        "host": "196.30.53.61",
        "port": 22,
        "user": "mapit",
        "remote_base": "/uploads",
        "quarters": "all",
        "datasets": {
            "MN_GLOBAL_MEA": "/mnt/data/DISTRIBUTION_CURRENT/MN/MN_MEA",
            "MN_GLOBAL_EUR": "/mnt/data/DISTRIBUTION_CURRENT/MN/MN_EUR",
            "MN_GLOBAL_NAM": "/mnt/data/DISTRIBUTION_CURRENT/MN/MN_NAM",
            "MN_GLOBAL_LAM": "/mnt/data/DISTRIBUTION_CURRENT/MN/MN_LAM",
            "MN_GLOBAL_SEA": "/mnt/data/DISTRIBUTION_CURRENT/MN/MN_SEA",
            "MN_GLOBAL_CAS": "/mnt/data/DISTRIBUTION_CURRENT/MN/MN_CAS",
            "MN_GLOBAL_IND": "/mnt/data/DISTRIBUTION_CURRENT/MN/MN_IND",
            "MN_GLOBAL_OCE": "/mnt/data/DISTRIBUTION_CURRENT/MN/MN_OCE",
            "MN_GLOBAL_ISR": "/mnt/data/DISTRIBUTION_CURRENT/MN/MN_ISR",
            "MN_GLOBAL_S_O": "/mnt/data/DISTRIBUTION_CURRENT/MN/MN_S_O",
        },
    },
    "autotrak_sftp": {
        "label": "Autotrak (SFTP)",
        "host": "196.30.53.61",
        "port": 22,
        "user": "mapit",
        "remote_base": "/uploads",
        "quarters": "all",
        "datasets": {
            "MN_GLOBAL_MEA": "/mnt/data/DISTRIBUTION_CURRENT/MN/MN_MEA",
            "MN_GLOBAL_EUR": "/mnt/data/DISTRIBUTION_CURRENT/MN/MN_EUR",
            "MN_GLOBAL_NAM": "/mnt/data/DISTRIBUTION_CURRENT/MN/MN_NAM",
            "MN_GLOBAL_LAM": "/mnt/data/DISTRIBUTION_CURRENT/MN/MN_LAM",
            "MN_GLOBAL_SEA": "/mnt/data/DISTRIBUTION_CURRENT/MN/MN_SEA",
            "MN_GLOBAL_CAS": "/mnt/data/DISTRIBUTION_CURRENT/MN/MN_CAS",
            "MN_GLOBAL_IND": "/mnt/data/DISTRIBUTION_CURRENT/MN/MN_IND",
            "MN_GLOBAL_OCE": "/mnt/data/DISTRIBUTION_CURRENT/MN/MN_OCE",
            "MN_GLOBAL_ISR": "/mnt/data/DISTRIBUTION_CURRENT/MN/MN_ISR",
            "MN_GLOBAL_S_O": "/mnt/data/DISTRIBUTION_CURRENT/MN/MN_S_O",
        },
    },
    "riskscape": {
        "label": "Riskscape",
        "host": "sftp.riskscape.pro",
        "port": 322,
        "user": "mapitsftp",
        "remote_base": "/MapitToRiskscape",
        "quarters": "odd",
        "datasets": {
            "MNR_MEA": "/mnt/data/DISTRIBUTION_CURRENT/MNR/MNR_MEA",
        },
        "pull_path": "/RiskscapeToMapit",
        "pull_dest": "/mnt/data/DISTRIBUTION_CURRENT/MAPIT",
    },
}

LAST_FOLDER_FILE = BASE_DIR / "data" / "last_push_folder.json"

def get_last_folder(client_id):
    try:
        if LAST_FOLDER_FILE.exists():
            data = json.loads(LAST_FOLDER_FILE.read_text())
            return data.get(client_id, "")
    except Exception:
        pass
    return ""

def save_last_folder(client_id, folder_name):
    try:
        LAST_FOLDER_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if LAST_FOLDER_FILE.exists():
            try:
                data = json.loads(LAST_FOLDER_FILE.read_text())
            except Exception:
                data = {}
        data[client_id] = folder_name
        LAST_FOLDER_FILE.write_text(json.dumps(data, indent=2))
    except Exception:
        pass


@app.route("/api/sftp/last_folder/<client_id>")
def api_last_folder(client_id):
    return jsonify({"folder_name": get_last_folder(client_id)})


def _sftp_password(client_id):
    env = load_env()
    key = f"SFTP_{client_id.upper()}_PASSWORD"
    return env.get(key, "")

def _count_files(path):
    try:
        return sum(len(files) for _, _, files in os.walk(path))
    except Exception:
        return 0

# ══════════════════════════════════════════════════════════════════════════════
#  FTP PUSH (Autotrak — plain FTP, not SFTP)
# ══════════════════════════════════════════════════════════════════════════════

FTP_CLIENTS = {
    "autotrak": {
        "label": "Autotrak (FTP)",
        "host": "196.30.53.61",
        "port": 21,
        "user": "mapit",
        "remote_base": "/uploads",
        "datasets": {
            "MN_GLOBAL_MEA": "/mnt/data/DISTRIBUTION_CURRENT/MN/MN_MEA",
            "MN_GLOBAL_EUR": "/mnt/data/DISTRIBUTION_CURRENT/MN/MN_EUR",
            "MN_GLOBAL_NAM": "/mnt/data/DISTRIBUTION_CURRENT/MN/MN_NAM",
            "MN_GLOBAL_LAM": "/mnt/data/DISTRIBUTION_CURRENT/MN/MN_LAM",
            "MN_GLOBAL_SEA": "/mnt/data/DISTRIBUTION_CURRENT/MN/MN_SEA",
            "MN_GLOBAL_CAS": "/mnt/data/DISTRIBUTION_CURRENT/MN/MN_CAS",
            "MN_GLOBAL_IND": "/mnt/data/DISTRIBUTION_CURRENT/MN/MN_IND",
            "MN_GLOBAL_OCE": "/mnt/data/DISTRIBUTION_CURRENT/MN/MN_OCE",
            "MN_GLOBAL_ISR": "/mnt/data/DISTRIBUTION_CURRENT/MN/MN_ISR",
            "MN_GLOBAL_S_O": "/mnt/data/DISTRIBUTION_CURRENT/MN/MN_S_O",
        },
    },
}

def _ftp_password(client_id):
    env = load_env()
    key = f"FTP_{client_id.upper()}_PASSWORD"
    return env.get(key, "")

@app.route("/api/ftp/status")
def ftp_status():
    push_status = load_push_status()
    result = {}
    for cid, cfg in FTP_CLIENTS.items():
        datasets = {}
        for name, local_path in cfg["datasets"].items():
            push_record = push_status.get(f"{cid}_{name}")
            datasets[name] = {
                "local_path": local_path,
                "file_count": _count_files(local_path),
                "exists": os.path.isdir(local_path),
                "last_pushed": push_record,
            }
        result[cid] = {
            "label": cfg["label"], "host": cfg["host"], "port": cfg["port"],
            "user": cfg["user"], "datasets": datasets,
            "has_password": bool(_ftp_password(cid)),
        }
    return jsonify(result)

@app.route("/api/ftp/test/<client_id>", methods=["POST"])
def ftp_test(client_id):
    if client_id not in FTP_CLIENTS:
        return jsonify({"error": "Unknown client"}), 404
    cfg = FTP_CLIENTS[client_id]
    pw = _ftp_password(client_id)
    if not pw:
        return jsonify({"ok": False, "msg": f"No password set. Add FTP_{client_id.upper()}_PASSWORD to config."})
    try:
        cmd = [
            "lftp", "-u", f"{cfg['user']},{pw}",
            "-p", str(cfg["port"]),
            f"ftp://{cfg['host']}",
            "-e", "pwd; bye"
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        ok = r.returncode == 0
        return jsonify({"ok": ok, "msg": (r.stdout or r.stderr).strip()[-300:]})
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "msg": "Connection timed out"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

@app.route("/api/ftp/push/<client_id>", methods=["POST"])
def ftp_push(client_id):
    if client_id not in FTP_CLIENTS:
        return jsonify({"error": "Unknown client"}), 404
    cfg = FTP_CLIENTS[client_id]
    pw = _ftp_password(client_id)
    data = request.get_json(silent=True) or {}
    folder_name = data.get("folder_name", "")
    dry_run = data.get("dry_run", False)
    if not pw:
        return jsonify({"error": f"No password set. Add FTP_{client_id.upper()}_PASSWORD to config."}), 400
    if not folder_name:
        return jsonify({"error": "folder_name required"}), 400

    job_id = str(uuid.uuid4())[:8]
    running_jobs[job_id] = {"status": "running", "output": []}

    log_file    = f"/var/log/ddq_ftp_{client_id}_{job_id}.log"
    script_file = f"/tmp/ddq_ftp_push_{job_id}.sh"

    dataset_lines = ""
    for name, local_path in cfg["datasets"].items():
        dataset_lines += f'push_ftp_dataset "{name}" "{local_path}"\n'

    script = f"""#!/bin/bash
HOST="{cfg['host']}"
PORT="{cfg['port']}"
USER="{cfg['user']}"
PASS="{pw}"
REMOTE_BASE="{cfg['remote_base']}/{folder_name}"
LOG="{log_file}"
DRY_RUN={1 if dry_run else 0}

log() {{ echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG"; }}

push_ftp_dataset() {{
  local DATASET="$1"
  local LOCAL_PATH="$2"
  local REMOTE_PATH="$REMOTE_BASE/$DATASET"
  log "── $DATASET ──"

  if [[ ! -d "$LOCAL_PATH" ]]; then
    log "   Skipping - local path missing: $LOCAL_PATH"
    return
  fi

  local COUNT
  COUNT=$(find "$LOCAL_PATH" -type f | wc -l)
  if [[ "$COUNT" -eq 0 ]]; then
    log "   Skipping - no files found"
    return
  fi
  log "   Found $COUNT file(s) to push"

  if [[ "$DRY_RUN" -eq 1 ]]; then
    find "$LOCAL_PATH" -type f | while read -r f; do
      log "   DRY-RUN would upload: $f -> $REMOTE_PATH/$(basename $f)"
    done
    return
  fi

  lftp -u "$USER,$PASS" -p "$PORT" "ftp://$HOST" <<LFTP_EOF >> "$LOG" 2>&1
set ftp:ssl-allow no
mkdir -f -p $REMOTE_BASE
mkdir -f -p $REMOTE_PATH
mirror -R --parallel=2 "$LOCAL_PATH" "$REMOTE_PATH"
bye
LFTP_EOF

  if [[ $? -eq 0 ]]; then
    log "   Done: $DATASET"
    bash /opt/nextcloud-setup/nextcloud-data-distribution/scripts/record_push_status.sh "{client_id}" "$DATASET" "$REMOTE_BASE" "$LOCAL_PATH" 2>>"$LOG" || true
    bash /opt/nextcloud-setup/nextcloud-data-distribution/scripts/record_push_status.sh "{client_id}" "$DATASET" "$REMOTE_BASE" "$LOCAL_PATH" 2>>"$LOG" || true
  else
    log "   FAILED: $DATASET"
  fi
}}

log "=== FTP Push - {cfg['label']} ==="
log "Remote: $REMOTE_BASE"
log "Started: $(date)"
log ""
{dataset_lines}
log ""
log "=== FTP push complete ==="
echo "__DONE__" >> "$LOG"
"""

    with open(script_file, 'w') as f:
        f.write(script)
    os.chmod(script_file, 0o755)

    running_jobs[job_id]["log_file"] = log_file
    running_jobs[job_id]["script_file"] = script_file

    def _push():
        def log(m): running_jobs[job_id]["output"].append(m)
        log(f"=== Pushing to {cfg['label']} ===")
        log(f"Remote folder: {cfg['remote_base']}/{folder_name}")
        log(f"Log: {log_file}")
        log("Running in background - safe to navigate away.")
        log("")

        with open(log_file, 'w') as lf:
            proc = subprocess.Popen(
                ["nohup", "bash", script_file],
                stdout=lf, stderr=lf,
                preexec_fn=os.setsid, close_fds=True
            )
        running_jobs[job_id]["pid"] = proc.pid
        log(f"PID: {proc.pid}")

        import time as _time
        last_line = 0
        while True:
            _time.sleep(2)
            try:
                with open(log_file, errors='replace') as lf:
                    lines = lf.readlines()
                for line in lines[last_line:]:
                    running_jobs[job_id]["output"].append(line.rstrip())
                last_line = len(lines)
                if any("__DONE__" in l for l in lines):
                    running_jobs[job_id]["status"] = "done"
                    if not dry_run:
                        send_notification_email(
                            f"GeoINT: FTP push to {cfg['label']} complete - {folder_name}",
                            f"<p>FTP push to <strong>{cfg['label']}</strong> completed.</p>"
                            f"<p>Remote folder: <code>/{folder_name}</code></p>"
                            f"<p>Log: <code>{log_file}</code></p>"
                        )
                    break
                if proc.poll() is not None and not any("__DONE__" in l for l in lines):
                    _time.sleep(2)
                    with open(log_file, errors='replace') as lf:
                        lines = lf.readlines()
                    for line in lines[last_line:]:
                        running_jobs[job_id]["output"].append(line.rstrip())
                    running_jobs[job_id]["status"] = "error"
                    break
            except Exception as e:
                running_jobs[job_id]["output"].append(f"Log tail error: {e}")
                break

    threading.Thread(target=_push, daemon=True).start()
    return jsonify({"job_id": job_id, "log_file": log_file})


def load_push_status():
    push_status_file = BASE_DIR / "data" / "push_status.json"
    try:
        if push_status_file.exists():
            return json.loads(push_status_file.read_text())
    except Exception:
        pass
    return {}



@app.route("/api/sftp/status")
def sftp_status():
    push_status = load_push_status()
    result = {}
    for cid, cfg in SFTP_CLIENTS.items():
        datasets = {}
        for name, local_path in cfg["datasets"].items():
            push_record = push_status.get(f"{cid}_{name}")
            datasets[name] = {
                "local_path": local_path,
                "file_count": _count_files(local_path),
                "exists": os.path.isdir(local_path),
                "last_pushed": push_record,
            }
        result[cid] = {
            "label": cfg["label"],
            "host": cfg["host"],
            "port": cfg["port"],
            "user": cfg["user"],
            "datasets": datasets,
            "has_password": bool(_sftp_password(cid)),
        }
    return jsonify(result)

@app.route("/api/sftp/remote_folders/<client_id>")
def sftp_remote_folders(client_id):
    """
    List folders that exist at the remote base path, so the operator can
    see what has actually been pushed before, instead of guessing from a
    local-only Ready/Empty status.
    """
    if client_id not in SFTP_CLIENTS:
        return jsonify({"error": "Unknown client"}), 404
    cfg = SFTP_CLIENTS[client_id]
    pw = _sftp_password(client_id)
    if not pw:
        return jsonify({"error": f"No password set. Add SFTP_{client_id.upper()}_PASSWORD to config."}), 400
    try:
        cmd = [
            "sshpass", "-p", pw,
            "ssh", "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10",
            "-p", str(cfg["port"]),
            f"{cfg['user']}@{cfg['host']}",
            f"ls -1 {cfg['remote_base']} 2>/dev/null"
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        folders = [f.strip() for f in r.stdout.splitlines() if f.strip()]
        return jsonify({"folders": sorted(folders, reverse=True)})
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Connection timed out"}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sftp/verify_push/<client_id>", methods=["POST"])
def sftp_verify_push(client_id):
    """
    Check whether a specific remote folder actually contains the expected
    datasets, by comparing remote file/subdir counts against local. This
    gives an honest answer to "has this already been pushed?" instead of
    only showing local readiness.
    """
    if client_id not in SFTP_CLIENTS:
        return jsonify({"error": "Unknown client"}), 404
    cfg = SFTP_CLIENTS[client_id]
    pw = _sftp_password(client_id)
    folder_name = request.json.get("folder_name", "") if request.is_json else ""
    if not pw:
        return jsonify({"error": f"No password set. Add SFTP_{client_id.upper()}_PASSWORD to config."}), 400
    if not folder_name:
        return jsonify({"error": "folder_name required"}), 400

    results = {}
    for name, local_path in cfg["datasets"].items():
        local_count = _count_files(local_path) if os.path.isdir(local_path) else 0
        remote_path = f"{cfg['remote_base']}/{folder_name}/{name}"
        try:
            cmd = [
                "sshpass", "-p", pw,
                "ssh", "-o", "StrictHostKeyChecking=no",
                "-o", "ConnectTimeout=10",
                "-p", str(cfg["port"]),
                f"{cfg['user']}@{cfg['host']}",
                f"find '{remote_path}' -type f 2>/dev/null | wc -l"
            ]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            remote_count = int(r.stdout.strip()) if r.stdout.strip().isdigit() else 0
        except Exception:
            remote_count = 0

        if remote_count == 0:
            status = "not_pushed"
        elif local_count > 0 and remote_count >= local_count:
            status = "pushed"
        elif remote_count > 0 and remote_count < local_count:
            status = "partially_pushed"
        else:
            status = "pushed"

        results[name] = {
            "local_count": local_count,
            "remote_count": remote_count,
            "status": status,
        }

    return jsonify({"folder_name": folder_name, "datasets": results})


@app.route("/api/sftp/test/<client_id>", methods=["POST"])
def sftp_test(client_id):
    if client_id not in SFTP_CLIENTS:
        return jsonify({"error": "Unknown client"}), 404
    cfg = SFTP_CLIENTS[client_id]
    pw  = _sftp_password(client_id)
    if not pw:
        return jsonify({"ok": False, "msg": f"No password set. Add SFTP_{client_id.upper()}_PASSWORD to config."}), 200
    cmd = [
        "sshpass", "-p", pw,
        "sftp", "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=8",
        "-P", str(cfg["port"]),
        f"{cfg['user']}@{cfg['host']}",
    ]
    try:
        r = subprocess.run(cmd, input="pwd\nbye\n", capture_output=True, text=True, timeout=15)
        ok = r.returncode == 0 or "sftp>" in r.stdout
        return jsonify({"ok": ok, "msg": r.stdout.strip() or r.stderr.strip()})
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "msg": "Connection timed out"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

@app.route("/api/sftp/push/<client_id>", methods=["POST"])
def sftp_push(client_id):
    if client_id not in SFTP_CLIENTS:
        return jsonify({"error": "Unknown client"}), 404
    cfg  = SFTP_CLIENTS[client_id]
    pw   = _sftp_password(client_id)
    data = request.get_json(silent=True) or {}
    folder_name = data.get("folder_name", "")
    only_datasets = data.get("datasets", None)
    if not pw:
        return jsonify({"error": f"No password set. Add SFTP_{client_id.upper()}_PASSWORD to config."}), 400
    if not folder_name:
        return jsonify({"error": "folder_name required"}), 400

    all_datasets = cfg["datasets"]
    if only_datasets:
        selected = {k: v for k, v in all_datasets.items() if k in only_datasets}
        if not selected:
            return jsonify({"error": "None of the requested datasets exist for this client"}), 400
    else:
        selected = all_datasets

    job_id = str(uuid.uuid4())[:8]
    running_jobs[job_id] = {"status": "running", "output": []}

    # Write push script to disk so it survives browser close / navigation
    log_file    = f"/var/log/ddq_sftp_{client_id}_{job_id}.log"
    script_file = f"/tmp/ddq_push_{job_id}.sh"

    # Build dataset list for the script - only the selected datasets
    dataset_lines = ""
    for name, local_path in selected.items():
        dataset_lines += f'push_dataset "{name}" "{local_path}"\n'

    script = f"""#!/bin/bash
HOST="{cfg['host']}"
PORT="{cfg['port']}"
USER="{cfg['user']}"
PASS="{pw}"
REMOTE_BASE="{cfg['remote_base']}/{folder_name}"
LOG="{log_file}"

log() {{ echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG"; }}
ssh_ls() {{ sshpass -p "$PASS" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=20 -p "$PORT" "$USER@$HOST" "ls $1 2>/dev/null" 2>/dev/null; }}

push_dataset() {{
  local DATASET="$1"
  local LOCAL_PATH="$2"
  local REMOTE_PATH="$REMOTE_BASE/$DATASET"
  local BATCH_FILE
  BATCH_FILE=$(mktemp /tmp/sftp_batch_XXXXXX)

  log "── $DATASET ──"

  # Get remote file list for skip logic (ls only — Windows Cygwin compatible)
  declare -A RMAP
  while IFS= read -r fname; do
    [[ -n "$fname" ]] && RMAP["$fname"]=1
  done < <(ssh_ls "$REMOTE_PATH/")

  local SKIP=0 UPLOAD=0
  while IFS= read -r LOCAL_FILE; do
    BASENAME=$(basename "$LOCAL_FILE")
    RELPATH="${{LOCAL_FILE#$LOCAL_PATH/}}"
    REMOTE_FILE="$REMOTE_PATH/$RELPATH"
    REMOTE_DIR=$(dirname "$REMOTE_FILE")
    if [[ -n "${{RMAP[$BASENAME]:-}}" ]]; then
      # Compare sizes
      LOCAL_SIZE=$(stat -c%s "$LOCAL_FILE" 2>/dev/null || echo 0)
      REMOTE_SIZE=$(sshpass -p "$PASS" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=20 -p "$PORT" "$USER@$HOST" "stat -c%s $REMOTE_FILE 2>/dev/null || echo 0" 2>/dev/null || echo 0)
      if [[ "$LOCAL_SIZE" == "$REMOTE_SIZE" ]]; then
        ((SKIP++))
        continue
      fi
      log "   REPLACE: $RELPATH (sizes differ)"
    else
      ((UPLOAD++))
    fi
    echo "-mkdir $REMOTE_DIR" >> "$BATCH_FILE"
    echo "put $LOCAL_FILE $REMOTE_FILE" >> "$BATCH_FILE"
  done < <(find "$LOCAL_PATH" -type f | sort)

  echo "bye" >> "$BATCH_FILE"
  log "   Skip: $SKIP  Upload/Replace: $UPLOAD"

  if [[ $UPLOAD -eq 0 && $SKIP -gt 0 ]]; then
    log "   ✅ $DATASET already complete on remote"
    rm -f "$BATCH_FILE"
    return
  fi

  log "   Uploading..."
  # Create top-level dataset folder first
  sshpass -p "$PASS" sftp -o StrictHostKeyChecking=no -o BatchMode=no -o ConnectTimeout=30 -P "$PORT" "$USER@$HOST" << SFTP_EOF >> "$LOG" 2>&1
-mkdir $REMOTE_BASE
-mkdir $REMOTE_PATH
bye
SFTP_EOF

  sshpass -p "$PASS" sftp \
    -o StrictHostKeyChecking=no \
    -o BatchMode=no \
    -o ConnectTimeout=30 \
    -P "$PORT" \
    "$USER@$HOST" < "$BATCH_FILE" >> "$LOG" 2>&1
  local RC=$?
  rm -f "$BATCH_FILE"
  if [[ $RC -eq 0 ]]; then
    log "   ✅ $DATASET done"
    FILE_COUNT_NOW=$(( SKIP + UPLOAD ))
    python3 -c "
import json, os
from pathlib import Path
status_file = Path('/opt/nextcloud-setup/nextcloud-data-distribution/data/push_status.json')
status_file.parent.mkdir(parents=True, exist_ok=True)
data = {{}}
if status_file.exists():
    try:
        data = json.loads(status_file.read_text())
    except Exception:
        data = {{}}
data['{client_id}_$DATASET'] = {{
    'client_id': '{client_id}',
    'dataset': '$DATASET',
    'folder_name': '$REMOTE_BASE'.split('/')[-1],
    'pushed_at': '$(date -Iseconds)',
    'file_count': $FILE_COUNT_NOW,
}}
status_file.write_text(json.dumps(data, indent=2))
" 2>>"$LOG"
  else
    log "   ❌ $DATASET failed (exit $RC)"
  fi
  unset RMAP
}}

log "━━━  SFTP Push — {cfg['label']}  ━━━"
log "Remote: $REMOTE_BASE"
log "Started: $(date)"
log ""
{dataset_lines}
log ""
log "━━━  Push complete — {cfg['label']}  ━━━"
echo "__DONE__" >> "$LOG"
"""

    with open(script_file, 'w') as f:
        f.write(script)
    os.chmod(script_file, 0o755)

    # Store job metadata
    running_jobs[job_id]["log_file"]    = log_file
    running_jobs[job_id]["script_file"] = script_file
    running_jobs[job_id]["client_id"]   = client_id
    running_jobs[job_id]["folder_name"] = folder_name

    def _push():
        def log(m): running_jobs[job_id]["output"].append(m)
        log(f"━━━  Pushing to {cfg['label']}  ━━━")
        log(f"Remote folder: {cfg['remote_base']}/{folder_name}")
        log(f"Log: {log_file}")
        log("Running in background — safe to navigate away or close browser.")
        log("This terminal reconnects automatically when you return to this page.")
        log("")

        # Launch fully detached — survives browser close
        with open(log_file, 'w') as lf:
            proc = subprocess.Popen(
                ["nohup", "bash", script_file],
                stdout=lf, stderr=lf,
                preexec_fn=os.setsid,
                close_fds=True
            )
        running_jobs[job_id]["pid"] = proc.pid
        log(f"PID: {proc.pid}")

        # Tail log file into job output
        import time as _time
        last_line = 0
        while True:
            _time.sleep(2)
            try:
                with open(log_file, errors='replace') as lf:
                    lines = lf.readlines()
                for line in lines[last_line:]:
                    running_jobs[job_id]["output"].append(line.rstrip())
                last_line = len(lines)
                # Check completion marker
                if any("__DONE__" in l for l in lines):
                    running_jobs[job_id]["status"] = "done"
                    send_notification_email(
                        f"✅ GeoINT: SFTP push to {cfg['label']} complete — {folder_name}",
                        f"<p>SFTP push to <strong>{cfg['label']}</strong> completed successfully.</p>"
                        f"<p>Folder: <code>{cfg['remote_base']}/{folder_name}</code></p>"
                        f"<p>Datasets: {', '.join(cfg['datasets'].keys())}</p>"
                        f"<p>Log: <code>{log_file}</code></p>"
                    )
                    break
                # Check if process ended unexpectedly
                if proc.poll() is not None and not any("__DONE__" in l for l in lines):
                    _time.sleep(2)
                    with open(log_file, errors='replace') as lf:
                        lines = lf.readlines()
                    for line in lines[last_line:]:
                        running_jobs[job_id]["output"].append(line.rstrip())
                    running_jobs[job_id]["status"] = "error"
                    break
            except Exception as e:
                running_jobs[job_id]["output"].append(f"Log tail error: {e}")
                break

    threading.Thread(target=_push, daemon=True).start()
    return jsonify({"job_id": job_id, "log_file": log_file})

@app.route("/api/sftp/pull/<client_id>", methods=["POST"])
def sftp_pull(client_id):
    if client_id not in SFTP_CLIENTS:
        return jsonify({"error": "Unknown client"}), 404
    cfg = SFTP_CLIENTS[client_id]
    if "pull_path" not in cfg:
        return jsonify({"error": "No pull path configured"}), 400
    pw = _sftp_password(client_id)
    if not pw:
        return jsonify({"error": f"No password set. Add SFTP_{client_id.upper()}_PASSWORD to config."}), 400

    job_id = str(uuid.uuid4())[:8]
    running_jobs[job_id] = {"status": "running", "output": []}

    def _pull():
        def log(m): running_jobs[job_id]["output"].append(m)
        log(f"━━━  Pulling from {cfg['label']}  ━━━")
        log(f"Remote: {cfg['pull_path']}")
        log(f"Local:  {cfg['pull_dest']}")
        log("")
        os.makedirs(cfg["pull_dest"], exist_ok=True)
        cmd = [
            "sshpass", "-p", pw,
            "rsync", "-avz", "--progress",
            "-e", f"ssh -p {cfg['port']} -o StrictHostKeyChecking=no",
            f"{cfg['user']}@{cfg['host']}:{cfg['pull_path']}/",
            f"{cfg['pull_dest']}/",
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
        if r.returncode == 0:
            log("✅ Pull complete")
        else:
            log(f"❌ Pull failed: {r.stderr.strip()[-500:]}")
        log(r.stdout.strip()[-1000:])
        running_jobs[job_id]["status"] = "done"

    threading.Thread(target=_pull, daemon=True).start()
    return jsonify({"job_id": job_id})

if __name__ == "__main__":
    print("\n" + "=" * 52)
    print("  GeoInt Distribution Dashboard v5")
    print(f"  http://0.0.0.0:5050")
    print("=" * 52 + "\n")
    app.run(host="0.0.0.0", port=5050, debug=False)
