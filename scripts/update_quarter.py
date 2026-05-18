#!/usr/bin/env python3
"""
update_quarter.py
─────────────────
Run at the start of each new quarter to:
  - Compare new CSV against previous CSV
  - Revoke access for clients/paths removed
  - Add access for new clients/paths added
  - Rotate passwords if they changed

Usage:
  python3 scripts/update_quarter.py \\
      --env config/.env \\
      --old  data/prev_quarter.csv \\
      --new  data/client_distribution.csv \\
      --dry-run
"""

import argparse
import sys
import pandas as pd
from pathlib import Path
from dotenv import load_dotenv
import subprocess, os, json

GREEN = "\033[0;32m"; YELLOW = "\033[1;33m"; RED = "\033[0;31m"
CYAN  = "\033[0;36m"; BOLD   = "\033[1m";    NC  = "\033[0m"

def info(m):    print(f"{CYAN}[INFO]{NC}  {m}")
def ok(m):      print(f"{GREEN}[OK]{NC}    {m}")
def warn(m):    print(f"{YELLOW}[WARN]{NC}  {m}")
def error(m):   print(f"{RED}[ERROR]{NC} {m}"); sys.exit(1)
def section(m): print(f"\n{BOLD}{GREEN}━━━  {m}  ━━━{NC}\n")

NC_PATH = "/var/www/html/nextcloud"

def occ(args, dry_run=False):
    cmd = ["sudo", "-u", "www-data", "php", f"{NC_PATH}/occ"] + args
    if dry_run:
        print(f"  {YELLOW}[DRY-RUN]{NC} {' '.join(cmd)}")
        return ""
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.stdout.strip()


def load_and_clean(path):
    df = pd.read_csv(path)
    df["CLIENT"]   = df["CLIENT"].ffill()
    df["PASSWORD"] = df["PASSWORD"].ffill()
    df = df.dropna(subset=["CLIENT", "GROUP", "DirectoryPath"])
    for col in df.columns:
        df[col] = df[col].astype(str).str.strip()
    return df


def build_client_map(df):
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env",     default="config/.env")
    parser.add_argument("--old",     required=True, help="Previous quarter CSV")
    parser.add_argument("--new",     required=True, help="New quarter CSV")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    load_dotenv(args.env)

    old_df  = load_and_clean(args.old)
    new_df  = load_and_clean(args.new)
    old_map = build_client_map(old_df)
    new_map = build_client_map(new_df)

    # ── Clients removed entirely ───────────────────────────────
    section("Removed Clients")
    removed = set(old_map) - set(new_map)
    if removed:
        for name in removed:
            username = old_map[name]["username"]
            warn(f"Disabling user: {username}  ({name})")
            occ(["user:disable", username], args.dry_run)
    else:
        info("No clients removed")

    # ── New clients added ──────────────────────────────────────
    section("New Clients")
    added = set(new_map) - set(old_map)
    if added:
        for name in added:
            c = new_map[name]
            ok(f"New client: {name}  →  username: {c['username']}")
            # Delegate to provision script logic (import inline)
            env = os.environ.copy()
            env["OC_PASS"] = c["password"]
            cmd = ["sudo", "-u", "www-data", "php", f"{NC_PATH}/occ",
                   "user:add", "--password-from-env",
                   f"--display-name={name}", c["username"]]
            if args.dry_run:
                print(f"  {YELLOW}[DRY-RUN]{NC} {' '.join(cmd)}")
            else:
                subprocess.run(cmd, env=env, capture_output=True)
            for grp in c["groups"]:
                occ(["group:adduser", grp, c["username"]], args.dry_run)
    else:
        info("No new clients")

    # ── Existing clients — check for group/path changes ────────
    section("Updated Client Access")
    for name in set(old_map) & set(new_map):
        old_c = old_map[name]
        new_c = new_map[name]
        username = new_c["username"]

        # Groups added
        for grp in new_c["groups"] - old_c["groups"]:
            ok(f"{name}: adding group {grp}")
            occ(["group:adduser", grp, username], args.dry_run)

        # Groups removed
        for grp in old_c["groups"] - new_c["groups"]:
            warn(f"{name}: removing group {grp}")
            occ(["group:removeuser", grp, username], args.dry_run)

        # Password changed
        if old_c["password"] != new_c["password"]:
            ok(f"{name}: updating password")
            env = os.environ.copy()
            env["OC_PASS"] = new_c["password"]
            cmd = ["sudo", "-u", "www-data", "php", f"{NC_PATH}/occ",
                   "user:resetpassword", "--password-from-env", username]
            if args.dry_run:
                print(f"  {YELLOW}[DRY-RUN]{NC} {' '.join(cmd)}")
            else:
                subprocess.run(cmd, env=env, capture_output=True)

    section("Done")
    ok("Quarter update complete. Run a file scan if new directories were added:")
    print("  sudo -u www-data php /var/www/html/nextcloud/occ files:scan --all")


if __name__ == "__main__":
    main()
