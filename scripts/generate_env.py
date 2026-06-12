#!/usr/bin/env python3
"""
scripts/generate_env.py
───────────────────────
Generates .folders.env from quarterly YAML.
Only marks a dataset as downloaded if its OWN family folder exists on disk.
"""

import sys
from datetime import datetime
from pathlib import Path

import yaml

BASE_DIR    = Path(__file__).parent.parent
QUARTERLY   = BASE_DIR / "quarterly"
FOLDERS_ENV = BASE_DIR / ".folders.env"
DOWNLOADS   = Path("/mnt/data/downloads/downloads/downloads")  # Mapflow puts files here

# Exact folder name pattern per family
FOLDER_PATTERNS = {
    "MNR":   lambda r, v: f"MultiNet-R_{r}_{v}_full_commercial",
    "MN":    lambda r, v: f"MultiNet_{r}_{v}_full_commercial",
    "MNSP":  lambda r, v: f"MultiNet-SpeedProfile_{r}_{v}_full_commercial",
    "MNPOI": lambda r, v: f"MultiNet-POI_{r}_{v}_full_commercial",
    "MNAP":  lambda r, v: f"MultiNet-AddressPoints_{r}_{v}_full_commercial",
    "MAPIT": lambda r, v: None,  # Never auto-detected — manual SFTP
}

# Sub-path inside the folder where data lives
DATA_SUFFIX = {
    "MNR":   lambda r: f"data/{r.lower()}",
    "MN":    lambda r: "data",           # MN files are flat in data/ not data/mea/
    "MNSP":  lambda r: "data",           # MNSP files are flat in data/ not data/mea/
    "MNPOI": lambda r: "data",           # MNPOI files are flat in data/ not data/mea/
    "MNAP":  lambda r: f"data/{r.lower()}",
    "MAPIT": lambda r: r,
}

# Datasets that are subsets of a larger regional download
# Key = dataset key, Value = (family, source_region)
SUBSET_SOURCE = {
    "MNR_SOUTHERN_AFRICA": ("MNR", "MEA"),
    "MNR_ZAF":             ("MNR", "MEA"),
    "MNR_ZAF_PLUS":        ("MNR", "MEA"),
    "POI_SOUTHERN_AFRICA": ("MNPOI", "MEA"),
    "POI_ZAF":             ("MNPOI", "MEA"),
}


def get_prefix(key):
    return key.split("_")[0]


def dataset_in_distro(distro_folder, key):
    """Check if dataset has already been ingested into the distribution folder."""
    distro = Path(distro_folder)
    # Map dataset keys to their distribution paths
    paths = {
        "MNR_MEA":            distro / "MNR" / "MNR_MEA",
        "MNR_EUR":            distro / "MNR" / "MNR_EUR",
        "MNR_SOUTHERN_AFRICA":distro / "MNR" / "MNR_SOUTHERN_AFRICA",
        "MNR_ZAF":            distro / "MNR" / "MNR_ZAF",
        "MNR_ZAF_PLUS":       distro / "MNR" / "MNR_ZAF_PLUS",
        "MN_MEA":             distro / "MN"  / "MN_MEA",
        "MN_EUR":             distro / "MN"  / "MN_EUR",
        "MN_3D_TRACKING":     distro / "MN"  / "MN_3D_TRACKING",
        "SP_MEA":             distro / "PRODUCTS" / "SPEED_PROFILES" / "SPEED_PROFILES_MEA",
        "SP_EUR":             distro / "PRODUCTS" / "SPEED_PROFILES" / "SPEED_PROFILES_EUR",
        "POI_MEA":            distro / "PRODUCTS" / "MN_PREMIUM_POI" / "MN_PREMIUM_POI_MEA",
        "POI_ZAF":            distro / "PRODUCTS" / "MN_PREMIUM_POI" / "MN_PREMIUM_POI_ZAF",
        "POI_SOUTHERN_AFRICA":distro / "PRODUCTS" / "MN_PREMIUM_POI" / "MN_PREMIUM_POI_SOUTHERN_AFRICA",
        "APT_MEA":            distro / "PRODUCTS" / "MN_APT" / "MN_APT_MEA",
    }
    p = paths.get(key)
    if p and p.exists():
        try:
            count = sum(1 for _ in p.rglob("*") if _.is_file())
            return count > 3
        except:
            pass
    return False

def get_version_for_family(family, version):
    """MNR uses .003 suffix, all other products use .000"""
    if family == "MNR":
        return version
    # Replace last 3 digits with 000 for non-MNR products
    parts = version.rsplit(".", 1)
    return f"{parts[0]}.000" if len(parts) == 2 else version

def folder_exists_on_disk(family, region, version):
    """
    Check if the download folder for this SPECIFIC family+region exists.
    Strict match — MultiNet-R_MEA will NOT match for MN, SP, POI, APT.
    """
    if family == "MAPIT":
        return False, None

    pattern_fn = FOLDER_PATTERNS.get(family)
    if not pattern_fn:
        return False, None

    folder_name = pattern_fn(region, version)
    if not folder_name:
        return False, None

    full_path = DOWNLOADS / folder_name

    if full_path.exists():
        # Verify it has actual data files — check tar.gz, 7z, zip
        try:
            for pattern in ["*.tar.gz", "*.7z.001", "*.zip"]:
                if sum(1 for _ in full_path.rglob(pattern)) > 0:
                    return True, folder_name
            if sum(1 for _ in full_path.rglob("*") if _.is_file()) > 5:
                return True, folder_name
        except:
            pass

    return False, folder_name


def generate_folders_env(quarter, dry_run=False, output_path=None):
    config_path = QUARTERLY / f"{quarter}.yaml"
    if not config_path.exists():
        print(f"❌ Config not found: {config_path}")
        sys.exit(1)

    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    distro   = cfg["distro_folder"]
    downloads = cfg["downloads_folder"]
    version  = cfg["tomtom_version"]
    datasets = cfg["datasets"]

    lines = [
        f"# ============================================================",
        f"# .folders.env — AUTO-GENERATED from quarterly/{quarter}.yaml",
        f"# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"# Edit quarterly/{quarter}.yaml — not this file.",
        f"# ============================================================",
        f"",
        f"DISTRO_FOLDER={distro}",
        f"DATA_FOLDER={downloads}",
        f"",
    ]

    active  = []
    pending = []

    for key, dataset in datasets.items():
        family = dataset.get("family", get_prefix(key))
        region = dataset.get("region", "MEA")

        # MAPIT — always pending (manual download)
        if family == "MAPIT":
            mapit_region = dataset.get("region", "ZAF")
            pending.append((key, f"mapit/{mapit_region}",
                           "Manual SFTP download — not auto-detected"))
            continue

        # Subset datasets — check their parent source
        if key in SUBSET_SOURCE:
            src_family, src_region = SUBSET_SOURCE[key]
            exists, folder = folder_exists_on_disk(src_family, src_region, get_version_for_family(src_family, version))
            suffix_fn = DATA_SUFFIX.get(src_family, lambda r: "")
            suffix = suffix_fn(region)
            src_path = f"{folder}/{suffix}".rstrip("/") if folder else ""
            already_ingested = dataset_in_distro(distro, key)
            if exists and src_path:
                zones = dataset.get("zones")
                zone_str = ""
                if zones and zones != "~ALL" and isinstance(zones, list):
                    zone_str = ",".join(z.lower() for z in zones)
                active.append((key, src_path,
                               f"Subset of {src_family}_{src_region}", zone_str))
            elif already_ingested:
                zones = dataset.get("zones")
                zone_str = ""
                if zones and zones != "~ALL" and isinstance(zones, list):
                    zone_str = ",".join(z.lower() for z in zones)
                active.append((key, src_path or f"{src_family}_{src_region}",
                               f"Subset ingested", zone_str))
            else:
                pending.append((key, src_path or f"{src_family}_{src_region} not downloaded yet",
                               f"Needs {src_family}_{src_region} download first", ""))
            continue

        # Normal dataset — check its own family folder
        exists, folder = folder_exists_on_disk(family, region, get_version_for_family(family, version))
        suffix_fn = DATA_SUFFIX.get(family, lambda r: "")
        suffix = suffix_fn(region)
        src_path = f"{folder}/{suffix}".rstrip("/") if folder else ""
        already_ingested = dataset_in_distro(distro, key)
        if exists and src_path:
            zones = dataset.get("zones")
            zone_str = ""
            if zones and zones != "~ALL" and isinstance(zones, list):
                zone_str = ",".join(z.lower() for z in zones)
            dest_subfolder = dataset.get("dest_subfolder", "")
            active.append((key, src_path, f"{family} {region}", zone_str, dest_subfolder))
        elif already_ingested:
            pattern_fn = FOLDER_PATTERNS.get(family)
            expected_folder = pattern_fn(region, version) if pattern_fn else f"{family}_{region}"
            expected_path = f"{expected_folder}/{suffix}".rstrip("/")
            zones = dataset.get("zones")
            zone_str = ""
            if zones and zones != "~ALL" and isinstance(zones, list):
                zone_str = ",".join(z.lower() for z in zones)
            dest_subfolder = dataset.get("dest_subfolder", "")
            active.append((key, expected_path, f"{family} {region} (ingested)", zone_str, dest_subfolder))
        else:
            pattern_fn = FOLDER_PATTERNS.get(family)
            expected = pattern_fn(region, version) if pattern_fn else f"{family}_{region}"
            pending.append((key, f"{expected}/data/{region.lower()}",
                           f"Not yet downloaded", ""))
    # Write active datasets
    if active:
        lines.append("# ── Downloaded — ready to ingest ─────────────────────────────")
        for item in active:
            key, src, note = item[0], item[1], item[2]
            zone_str = item[3] if len(item) > 3 else ""
            lines.append(f"{key}={src}")
            if zone_str:
                lines.append(f"{key}_ZONES={zone_str}")
            dest_subfolder = item[4] if len(item) > 4 else ""
            if dest_subfolder:
                lines.append(f"{key}_DEST_SUBFOLDER={dest_subfolder}")
        lines.append("")

    # Write pending as comments
    if pending:
        lines.append("# ── Pending — not yet downloaded ──────────────────────────────")
        for item in pending:
            key, src, note = item[0], item[1], item[2]
            lines.append(f"# {key}={src}  # {note}")
        lines.append("")

    content = "\n".join(lines)

    if dry_run:
        print(content)
        return content

    out = output_path or FOLDERS_ENV
    out.write_text(content)
    print(f"✅ Generated: {out}")
    print(f"   Active  : {len(active)} dataset(s)")
    print(f"   Pending : {len(pending)} dataset(s)")
    return content


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--quarter", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    generate_folders_env(args.quarter, dry_run=args.dry_run)
