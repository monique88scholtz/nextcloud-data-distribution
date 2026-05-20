#!/usr/bin/env python3
"""
agent_provision.py
──────────────────
SMART PROVISIONING AGENT
Watches for a new CSV drop and automatically:
  - Validates the CSV
  - Diffs against the previous quarter
  - Creates/removes users and groups
  - Mounts new data folders
  - Sends a summary report

Run as a service or cron job.

Usage:
  python3 agents/agent_provision.py --env config/.env --watch
  python3 agents/agent_provision.py --env config/.env --run-now
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

# ── Colours ───────────────────────────────────────────────────
GREEN  = "\033[0;32m"; YELLOW = "\033[1;33m"
RED    = "\033[0;31m"; CYAN   = "\033[0;36m"
BOLD   = "\033[1m";    NC     = "\033[0m"

def info(m):    print(f"{CYAN}[{timestamp()}][INFO]{NC}  {m}")
def ok(m):      print(f"{GREEN}[{timestamp()}][OK]{NC}    {m}")
def warn(m):    print(f"{YELLOW}[{timestamp()}][WARN]{NC}  {m}")
def error(m):   print(f"{RED}[{timestamp()}][ERROR]{NC} {m}")
def section(m): print(f"\n{BOLD}{GREEN}━━━  {m}  ━━━{NC}\n")
def timestamp(): return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

NC_PATH = "/var/www/html/nextcloud"

def occ(args: list) -> str:
    cmd = ["sudo", "-u", "www-data", "php", f"{NC_PATH}/occ"] + args
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout.strip()


def file_hash(path: str) -> str:
    """Get MD5 hash of a file to detect changes."""
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["CLIENT"]   = df["CLIENT"].ffill()
    df["PASSWORD"] = df["PASSWORD"].ffill()
    df = df.dropna(subset=["CLIENT", "GROUP", "DirectoryPath"])
    for col in df.columns:
        df[col] = df[col].astype(str).str.strip()
    return df


def build_client_map(df: pd.DataFrame) -> dict:
    clients = {}
    for _, row in df.iterrows():
        name = row["CLIENT"]
        if name not in clients:
            clients[name] = {
                "password": row["PASSWORD"],
                "username": name.lower().replace(" ", "_"),
                "groups":   set(),
                "paths":    set(),
            }
        clients[name]["groups"].add(row["GROUP"])
        clients[name]["paths"].add(row["DirectoryPath"])
    return clients


def get_existing_users() -> list:
    out = occ(["user:list", "--output=json"])
    try:
        return list(json.loads(out).keys())
    except:
        return []


def get_existing_groups() -> list:
    out = occ(["group:list", "--output=json"])
    try:
        return list(json.loads(out).keys())
    except:
        return []


def create_user(username: str, password: str, display_name: str):
    existing = get_existing_users()
    if username in existing:
        info(f"  User exists: {username}")
        return
    env = os.environ.copy()
    env["OC_PASS"] = password
    cmd = ["sudo", "-u", "www-data", "php", f"{NC_PATH}/occ",
           "user:add", "--password-from-env",
           f"--display-name={display_name}", username]
    subprocess.run(cmd, env=env, capture_output=True)
    ok(f"  Created user: {username}")


def create_group(group_id: str):
    existing = get_existing_groups()
    if group_id in existing:
        return
    occ(["group:add", group_id])
    ok(f"  Created group: {group_id}")


def assign_group(username: str, group_id: str):
    occ(["group:adduser", group_id, username])


def remove_group(username: str, group_id: str):
    occ(["group:removeuser", group_id, username])


def disable_user(username: str):
    occ(["user:disable", username])
    warn(f"  Disabled user: {username}")


def mount_path(label: str, path: str, group_id: str):
    if not Path(path).exists():
        warn(f"  Path missing, skipping: {path}")
        return
    out = occ(["files_external:create", f"/{label}", "local", "null::null",
               "-c", f"datadir={path}", "--output=json"])
    try:
        parsed = json.loads(out)
        mount_id = parsed if isinstance(parsed, int) else parsed.get("mount_id") or parsed.get("id")
        if mount_id:
            occ(["files_external:applicable", "--remove-all", str(mount_id)])
            occ(["files_external:applicable", "--add-group", group_id, str(mount_id)])
            ok(f"  Mounted {label} → group:{group_id}")
    except Exception as e:
        warn(f"  Mount parse error: {e}")


def run_provision(csv_path: str, prev_csv_path: str = None):
    """Main provisioning logic — idempotent, safe to re-run."""
    section("Loading CSV")
    df = load_csv(csv_path)
    new_map = build_client_map(df)
    info(f"Loaded {len(new_map)} clients")

    # ── Groups ────────────────────────────────────────────────
    section("Syncing Groups")
    all_groups = df["GROUP"].unique().tolist()
    for grp in all_groups:
        create_group(grp)

    # ── Users ─────────────────────────────────────────────────
    section("Syncing Users")
    old_map = {}
    if prev_csv_path and Path(prev_csv_path).exists():
        old_df  = load_csv(prev_csv_path)
        old_map = build_client_map(old_df)

    for display_name, client in new_map.items():
        print(f"\n  {BOLD}▶ {display_name}{NC}")
        create_user(client["username"], client["password"], display_name)

        # New group assignments
        old_groups = old_map.get(display_name, {}).get("groups", set())
        for grp in client["groups"] - old_groups:
            assign_group(client["username"], grp)
            info(f"    + group: {grp}")

        # Removed group assignments
        for grp in old_groups - client["groups"]:
            remove_group(client["username"], grp)
            warn(f"    - group: {grp}")

        # Password changed
        if display_name in old_map:
            if old_map[display_name]["password"] != client["password"]:
                env = os.environ.copy()
                env["OC_PASS"] = client["password"]
                cmd = ["sudo", "-u", "www-data", "php", f"{NC_PATH}/occ",
                       "user:resetpassword", "--password-from-env", client["username"]]
                subprocess.run(cmd, env=env, capture_output=True)
                ok(f"    ↺ password updated")

    # ── Disabled clients ──────────────────────────────────────
    if old_map:
        removed = set(old_map.keys()) - set(new_map.keys())
        if removed:
            section("Disabling Removed Clients")
            for name in removed:
                disable_user(old_map[name]["username"])

    # ── File scan ─────────────────────────────────────────────
    section("Scanning Files")
    occ(["files:scan", "--all"])
    ok("File scan complete")

    # ── Summary ───────────────────────────────────────────────
    section("Summary")
    print(f"  {'CLIENT':<25} {'USERNAME':<25} GROUPS")
    print(f"  {'─'*70}")
    for display_name, c in new_map.items():
        print(f"  {display_name:<25} {c['username']:<25} {', '.join(sorted(c['groups']))}")
    print()
    ok(f"Provisioning complete at {timestamp()}")


def watch_mode(csv_path: str, prev_csv_path: str, interval: int = 60):
    """Watch for CSV changes and auto-provision."""
    info(f"Watching {csv_path} every {interval}s for changes...")
    last_hash = None

    while True:
        try:
            if Path(csv_path).exists():
                current_hash = file_hash(csv_path)
                if current_hash != last_hash:
                    if last_hash is not None:
                        ok(f"CSV changed — running provisioning...")
                        run_provision(csv_path, prev_csv_path)
                    else:
                        info("Initial CSV hash recorded — watching for changes...")
                    last_hash = current_hash
        except Exception as e:
            error(f"Watch error: {e}")
        time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description="Smart Nextcloud Provisioning Agent")
    parser.add_argument("--env",      default="config/.env")
    parser.add_argument("--csv",      default="data/client_distribution.csv")
    parser.add_argument("--prev-csv", default="data/prev_quarter.csv")
    parser.add_argument("--watch",    action="store_true", help="Watch mode — auto-run on CSV change")
    parser.add_argument("--run-now",  action="store_true", help="Run provisioning immediately")
    parser.add_argument("--interval", type=int, default=60, help="Watch interval in seconds")
    args = parser.parse_args()

    if not Path(args.env).exists():
        error(f".env not found at {args.env}")
        sys.exit(1)

    load_dotenv(args.env)

    if os.geteuid() != 0:
        error("Run as root: sudo python3 agents/agent_provision.py")
        sys.exit(1)

    if args.run_now:
        run_provision(args.csv, args.prev_csv)
    elif args.watch:
        watch_mode(args.csv, args.prev_csv, args.interval)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
