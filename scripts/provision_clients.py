#!/usr/bin/env python3
"""
provision_clients.py
────────────────────
Reads the quarterly distribution CSV and automatically:
  1. Creates a Nextcloud group per unique GROUP value
  2. Creates a Nextcloud user per CLIENT (with their UUID password)
  3. Assigns the user to their groups
  4. Mounts each DirectoryPath as external storage scoped to the right group
  5. Optionally sends a summary report

Usage:
  python3 scripts/provision_clients.py --env config/.env --csv data/client_distribution.csv
  python3 scripts/provision_clients.py --env config/.env --csv data/client_distribution.csv --dry-run

Requirements:
  pip install pandas python-dotenv requests
"""

import argparse
import os
import subprocess
import sys
import json
import pandas as pd
from pathlib import Path
from dotenv import load_dotenv

# ── Colour helpers ────────────────────────────────────────────
GREEN  = "\033[0;32m"; YELLOW = "\033[1;33m"
RED    = "\033[0;31m"; CYAN   = "\033[0;36m"
BOLD   = "\033[1m";    NC     = "\033[0m"

def info(msg):    print(f"{CYAN}[INFO]{NC}  {msg}")
def ok(msg):      print(f"{GREEN}[OK]{NC}    {msg}")
def warn(msg):    print(f"{YELLOW}[WARN]{NC}  {msg}")
def error(msg):   print(f"{RED}[ERROR]{NC} {msg}"); sys.exit(1)
def section(msg): print(f"\n{BOLD}{GREEN}━━━  {msg}  ━━━{NC}\n")


# ── OCC helper ────────────────────────────────────────────────
NC_PATH = "/var/www/html/nextcloud"

def occ(args: list, dry_run=False) -> str:
    """Run an occ command as www-data and return stdout."""
    cmd = ["sudo", "-u", "www-data", "php", f"{NC_PATH}/occ"] + args
    if dry_run:
        print(f"  {YELLOW}[DRY-RUN]{NC} {' '.join(cmd)}")
        return ""
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        warn(f"occ error: {result.stderr.strip()}")
    return result.stdout.strip()


# ── Load & clean CSV ──────────────────────────────────────────
def load_csv(path: str, distro_folder: str = "") -> pd.DataFrame:
    df = pd.read_csv(path)
    # Forward-fill CLIENT and PASSWORD (they only appear on the first row per client)
    df["CLIENT"]   = df["CLIENT"].ffill()
    df["PASSWORD"] = df["PASSWORD"].ffill()
    # Drop fully empty rows
    df = df.dropna(subset=["CLIENT", "GROUP", "DirectoryPath"])
    # Clean whitespace
    for col in df.columns:
        df[col] = df[col].astype(str).str.strip()
    # Substitute {DISTRO} placeholder with the current quarter distro folder
    if distro_folder:
        df["DirectoryPath"] = df["DirectoryPath"].str.replace(
            "{DISTRO}", distro_folder, regex=False)
    return df


# ── Build client map ──────────────────────────────────────────
def build_client_map(df: pd.DataFrame) -> dict:
    """
    Returns:
    {
      "ClientName": {
        "password": "uuid",
        "username": "clientname",   # lowercase, spaces→underscore
        "groups":   ["group1", "group2"],
        "paths":    ["/mnt/data/...", ...]
      }
    }
    """
    clients = {}
    for _, row in df.iterrows():
        name = row["CLIENT"]
        if name not in clients:
            clients[name] = {
                "password": row["PASSWORD"],
                "username": name.lower().replace(" ", "_"),
                "groups":   [],
                "paths":    [],
                "data":     [],
            }
        grp = row["GROUP"]
        pth = row["DirectoryPath"]
        dat = row.get("DATA", "")
        if grp not in clients[name]["groups"]:
            clients[name]["groups"].append(grp)
        if pth not in clients[name]["paths"]:
            clients[name]["paths"].append(pth)
            clients[name]["data"].append(dat)
    return clients


# ── Provisioning functions ────────────────────────────────────
def create_group(group_id: str, dry_run: bool):
    existing = occ(["group:list", "--output=json"], dry_run)
    if not dry_run:
        try:
            groups = json.loads(existing) if existing else {}
            if group_id in groups:
                info(f"  Group already exists: {group_id}")
                return
        except json.JSONDecodeError:
            pass
    ok(f"  Creating group: {group_id}")
    occ(["group:add", group_id], dry_run)


def create_user(username: str, password: str, display_name: str, dry_run: bool):
    existing = occ(["user:list", "--output=json"], dry_run)
    if not dry_run:
        try:
            users = json.loads(existing) if existing else {}
            if username in users:
                info(f"  User already exists: {username}")
                return
        except json.JSONDecodeError:
            pass
    ok(f"  Creating user: {username}")
    occ(["user:add",
         "--password-from-env",
         f"--display-name={display_name}",
         username],
        dry_run)
    if not dry_run:
        env = os.environ.copy()
        env["OC_PASS"] = password
        cmd = ["sudo", "-u", "www-data", "php", f"{NC_PATH}/occ",
               "user:add", "--password-from-env",
               f"--display-name={display_name}", username]
        subprocess.run(cmd, env=env, capture_output=True)


def assign_group(username: str, group_id: str, dry_run: bool):
    info(f"  Assigning {username} → group:{group_id}")
    occ(["group:adduser", group_id, username], dry_run)


def mount_external_storage(group_id: str, path: str, label: str, dry_run: bool):
    """Mount a local directory as external storage visible to a group."""
    if not Path(path).exists() and not dry_run:
        warn(f"  Path does not exist on server, skipping: {path}")
        return
    folder_name = label.replace(" ", "_").replace("/", "_")
    info(f"  Mounting {path} → /{folder_name} (group: {group_id})")
    # Create the mount
    out = occ([
        "files_external:create",
        f"/{folder_name}",
        "local",
        "null::null",
        "-c", f"datadir={path}",
        "--output=json"
    ], dry_run)
    if not dry_run and out:
        try:
            parsed = json.loads(out)
            if isinstance(parsed, int):
                mount_id = parsed
            elif isinstance(parsed, dict):
                mount_id = parsed.get("mount_id") or parsed.get("id")
            else:
                mount_id = None

            if mount_id:
                occ(["files_external:applicable",
                     "--add-group", group_id,
                     str(mount_id)], dry_run)
                ok(f"  Mounted and scoped to group: {group_id}")
            else:
                warn(f"  Could not determine mount_id from: {out}")
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            warn(f"  Could not parse mount output ({e}): {out}")


# ── Main ──────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Provision Nextcloud users from CSV")
    parser.add_argument("--env",     default="config/.env",                    help="Path to .env file")
    parser.add_argument("--csv",     default="data/client_distribution.csv",   help="Path to distribution CSV")
    parser.add_argument("--dry-run", action="store_true",                      help="Print commands without running them")
    parser.add_argument("--report",  action="store_true",                      help="Print a summary report at the end")
    args = parser.parse_args()

    # Resolve distro folder from quarterly YAML if --quarter provided
    distro_folder = ""
    if args.quarter:
        import yaml as _yaml
        quarterly_path = Path(__file__).parent.parent / "quarterly" / f"{args.quarter}.yaml"
        if quarterly_path.exists():
            with open(quarterly_path) as _f:
                _cfg = _yaml.safe_load(_f)
            distro_folder = _cfg.get("distro_folder", "")
            info(f"Quarter: {args.quarter} → {distro_folder}")
        else:
            warn(f"Quarterly config not found: {quarterly_path}")

    # Load env
    if not Path(args.env).exists():
        error(f".env not found at {args.env}. Copy config/.env.template to config/.env and fill it in.")
    load_dotenv(args.env)

    # Must run as root (or with sudo access) unless dry-run
    if not args.dry_run and os.geteuid() != 0:
        error("Please run as root: sudo python3 scripts/provision_clients.py ...")

    # Load CSV
    if not Path(args.csv).exists():
        error(f"CSV not found at {args.csv}")
    df = load_csv(args.csv, distro_folder=distro_folder)
    client_map = build_client_map(df)

    info(f"Loaded {len(client_map)} clients from {args.csv}")
    if args.dry_run:
        warn("DRY-RUN mode — no changes will be made\n")

    # ── Step 1: Enable external storage app ───────────────────
    section("Step 1 — Enable External Storage App")
    occ(["app:enable", "files_external"], args.dry_run)
    ok("External storage app enabled")

    # ── Step 2: Create all groups ─────────────────────────────
    section("Step 2 — Create Groups")
    all_groups = df["GROUP"].unique().tolist()
    for grp in all_groups:
        create_group(grp, args.dry_run)

    # ── Step 3: Create users, assign groups, mount paths ──────
    section("Step 3 — Create Users & Assign Access")
    for display_name, client in client_map.items():
        print(f"\n{BOLD}  ▶ {display_name}{NC}")

        # Create user
        create_user(client["username"], client["password"], display_name, args.dry_run)

        # Assign to groups
        for grp in client["groups"]:
            assign_group(client["username"], grp, args.dry_run)

    # ── Step 4: Mount external storage per group ──────────────
    section("Step 4 — Mount Data Directories")
    # Build group → paths mapping
    group_paths: dict[str, list] = {}
    for _, row in df.iterrows():
        grp = row["GROUP"]
        pth = row["DirectoryPath"]
        dat = row.get("DATA", pth)
        if grp not in group_paths:
            group_paths[grp] = []
        if pth not in [x["path"] for x in group_paths[grp]]:
            group_paths[grp].append({"path": pth, "label": dat})

    for grp, entries in group_paths.items():
        for entry in entries:
            mount_external_storage(grp, entry["path"], entry["label"], args.dry_run)

    # ── Step 5: Rescan files ───────────────────────────────────
    section("Step 5 — Trigger File Scan")
    occ(["files:scan", "--all"], args.dry_run)
    ok("File scan triggered")

    # ── Summary report ─────────────────────────────────────────
    if args.report or args.dry_run:
        section("Summary Report")
        print(f"{'CLIENT':<25} {'USERNAME':<25} {'GROUPS':<45} PATHS")
        print("─" * 120)
        for display_name, c in client_map.items():
            groups_str = ", ".join(c["groups"])
            paths_str  = f"{len(c['paths'])} folder(s)"
            print(f"{display_name:<25} {c['username']:<25} {groups_str:<45} {paths_str}")

    print(f"\n{GREEN}{BOLD}✅  Provisioning complete!{NC}\n")


if __name__ == "__main__":
    main()
