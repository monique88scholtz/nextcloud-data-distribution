#!/usr/bin/env python3
"""
agent_refresh.py
────────────────
DATA REFRESH AGENT
Runs every minute (via cron or as a service) and:
  - Scans for new/changed files in mounted data directories
  - Triggers Nextcloud file cache refresh
  - Logs activity with timestamps
  - Alerts if a data directory goes missing

Usage:
  # Run once
  python3 agents/agent_refresh.py --env config/.env

  # Run as daemon (every 60s)
  python3 agents/agent_refresh.py --env config/.env --daemon

  # Add to cron (recommended):
  # * * * * * cd /opt/nextcloud-setup/nextcloud-data-distribution && \
  #   sudo .venv/bin/python3 agents/agent_refresh.py --env config/.env >> logs/refresh.log 2>&1
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

GREEN  = "\033[0;32m"; YELLOW = "\033[1;33m"
RED    = "\033[0;31m"; CYAN   = "\033[0;36m"
BOLD   = "\033[1m";    NC     = "\033[0m"

def ts():     return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
def info(m):  print(f"[{ts()}][INFO]  {m}", flush=True)
def ok(m):    print(f"[{ts()}][OK]    {m}", flush=True)
def warn(m):  print(f"[{ts()}][WARN]  {m}", flush=True)
def error(m): print(f"[{ts()}][ERROR] {m}", flush=True)

NC_PATH = "/var/www/html/nextcloud"

def occ(args: list) -> str:
    cmd = ["sudo", "-u", "www-data", "php", f"{NC_PATH}/occ"] + args
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout.strip()


def get_mounts() -> list:
    """Get all external storage mounts."""
    out = occ(["files_external:list", "--output=json"])
    try:
        return json.loads(out) if out else []
    except:
        return []


def check_mount_health(mounts: list) -> tuple:
    """Check which mounts are healthy and which are missing."""
    healthy = []
    missing = []
    for mount in mounts:
        config = mount.get("configuration", {})
        datadir = config.get("datadir", "")
        if datadir and Path(datadir).exists():
            healthy.append(mount)
        elif datadir:
            missing.append(mount)
    return healthy, missing


def scan_user(username: str) -> dict:
    """Scan files for a specific user and return stats."""
    out = occ(["files:scan", username, "--output=json"])
    try:
        return json.loads(out)
    except:
        return {}


def scan_all() -> None:
    """Trigger a full file scan for all users."""
    info("Starting full file scan...")
    result = occ(["files:scan", "--all"])
    ok("File scan complete")
    return result


def run_refresh(scan_users_list: list = None):
    """Main refresh logic."""
    info("─" * 50)
    info("Data Refresh Agent starting")

    # Check mount health
    mounts = get_mounts()
    if mounts:
        healthy, missing = check_mount_health(mounts)
        ok(f"Mounts healthy: {len(healthy)}")
        if missing:
            for m in missing:
                warn(f"Mount missing on disk: {m.get('mount_point')} → {m.get('configuration', {}).get('datadir')}")
    else:
        warn("Could not retrieve mount list")

    # Scan files
    if scan_users_list:
        for username in scan_users_list:
            info(f"Scanning: {username}")
            scan_user(username)
            ok(f"Scanned: {username}")
    else:
        scan_all()

    ok("Refresh complete")
    info("─" * 50)


def daemon_mode(interval: int, scan_users_list: list = None):
    """Run refresh on a loop."""
    info(f"Daemon mode — refreshing every {interval}s")
    while True:
        try:
            run_refresh(scan_users_list)
        except Exception as e:
            error(f"Refresh error: {e}")
        time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description="Nextcloud Data Refresh Agent")
    parser.add_argument("--env",     default="config/.env")
    parser.add_argument("--daemon",  action="store_true", help="Run continuously")
    parser.add_argument("--interval",type=int, default=60, help="Refresh interval in seconds")
    parser.add_argument("--users",   nargs="*", help="Specific users to scan (default: all)")
    args = parser.parse_args()

    load_dotenv(args.env)

    if os.geteuid() != 0:
        error("Run as root: sudo python3 agents/agent_refresh.py")
        sys.exit(1)

    # Create logs directory
    Path("logs").mkdir(exist_ok=True)

    if args.daemon:
        daemon_mode(args.interval, args.users)
    else:
        run_refresh(args.users)


if __name__ == "__main__":
    main()
