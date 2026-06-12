#!/usr/bin/env python3
"""
healthcheck.py
──────────────
ONE-COMMAND SYSTEM HEALTH CHECK
Checks every component of the Nextcloud stack and prints a clean report.

Usage:
  python3 scripts/healthcheck.py
  python3 scripts/healthcheck.py --json        # machine-readable output
  python3 scripts/healthcheck.py --notify      # post to Slack (if configured)

Add to cron for daily health reports:
  0 8 * * * cd /opt/nextcloud-setup/nextcloud-data-distribution && \
    sudo .venv/bin/python3 scripts/healthcheck.py >> logs/healthcheck.log 2>&1
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

# ── Colours ───────────────────────────────────────────────────
GREEN  = "\033[0;32m"; YELLOW = "\033[1;33m"
RED    = "\033[0;31m"; CYAN   = "\033[0;36m"
BOLD   = "\033[1m";    NC     = "\033[0m"

NC_PATH = "/var/www/html/nextcloud"
results = []


def ts(): return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def check(name: str, status: bool, detail: str = "", warn: bool = False):
    """Record and print a check result."""
    if status:
        icon = f"{GREEN}✅{NC}"
        state = "OK"
    elif warn:
        icon = f"{YELLOW}⚠️ {NC}"
        state = "WARN"
    else:
        icon = f"{RED}❌{NC}"
        state = "FAIL"
    print(f"  {icon}  {BOLD}{name:<20}{NC}  {detail}")
    results.append({"check": name, "status": state, "detail": detail})


def run(cmd: list) -> tuple:
    """Run a command, return (returncode, stdout, stderr)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return 1, "", str(e)


def occ(args: list) -> tuple:
    return run(["sudo", "-u", "www-data", "php", f"{NC_PATH}/occ"] + args)


def section(title: str):
    print(f"\n{BOLD}{CYAN}  {title}{NC}")
    print(f"  {'─' * 45}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env",    default="config/.env")
    parser.add_argument("--json",   action="store_true")
    args = parser.parse_args()

    load_dotenv(args.env)

    print(f"\n{BOLD}{'═' * 50}")
    print(f"  NEXTCLOUD HEALTH CHECK")
    print(f"  {ts()}")
    print(f"{'═' * 50}{NC}")

    # ── Services ──────────────────────────────────────────────
    section("Services")

    for service in ["apache2", "mariadb", "redis-server"]:
        code, out, _ = run(["systemctl", "is-active", service])
        check(service, code == 0, "running" if code == 0 else "STOPPED")

    # ── PHP ───────────────────────────────────────────────────
    section("PHP")
    code, out, _ = run(["php", "--version"])
    version = out.split("\n")[0] if out else "unknown"
    is_83 = "8.3" in version
    check("PHP version", is_83, version, warn=not is_83)

    required_exts = ["zip", "mbstring", "gd", "curl", "mysql", "xml", "intl", "bcmath"]
    code, out, _ = run(["php", "-m"])
    loaded = out.lower()
    missing = [e for e in required_exts if e not in loaded]
    check("PHP extensions", len(missing) == 0,
          "all loaded" if not missing else f"missing: {', '.join(missing)}")

    # ── Nextcloud ─────────────────────────────────────────────
    section("Nextcloud")

    code, out, _ = occ(["status", "--output=json"])
    try:
        status = json.loads(out)
        check("Installation",  status.get("installed", False), f"v{status.get('versionstring', '?')}")
        check("Maintenance",   not status.get("maintenance", True),
              "off" if not status.get("maintenance") else "ON — run: occ maintenance:mode --off",
              warn=status.get("maintenance", False))
        check("DB upgrade",    not status.get("needsDbUpgrade", True),
              "not needed" if not status.get("needsDbUpgrade") else "NEEDED — run: occ upgrade")
    except:
        check("Nextcloud status", False, "could not parse status")

    # ── Database ──────────────────────────────────────────────
    section("Database")

    db_name = os.getenv("DB_NAME", "nextcloud")
    db_user = os.getenv("DB_USER", "nextclouduser")
    db_pass = os.getenv("DB_PASSWORD", "")
    code, out, _ = run(["mysql", "-u", db_user, f"-p{db_pass}", "-e",
                         f"SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='{db_name}';"])
    check("DB connection", code == 0, f"database '{db_name}' accessible" if code == 0 else "connection failed")

    # ── Users ─────────────────────────────────────────────────
    section("Users & Groups")

    code, out, _ = occ(["user:list", "--output=json"])
    try:
        users = json.loads(out)
        user_count = len(users)
        check("User accounts", user_count > 1, f"{user_count} accounts")
    except:
        check("User accounts", False, "could not list users")

    code, out, _ = occ(["group:list", "--output=json"])
    try:
        groups = json.loads(out)
        check("Groups", len(groups) > 0, f"{len(groups)} groups")
    except:
        check("Groups", False, "could not list groups")

    # ── Mounts ────────────────────────────────────────────────
    section("External Storage Mounts")

    code, out, _ = occ(["files_external:list", "--output=json"])
    try:
        mounts = json.loads(out) if out else []
        total    = len(mounts)
        healthy  = 0
        broken   = []
        exposed  = []

        for m in mounts:
            config  = m.get("configuration", {})
            datadir = config.get("datadir", "")
            mount_point = m.get("mount_point", "")
            applicable_groups = m.get("applicable_groups", [])
            applicable_users  = m.get("applicable_users", [])

            # Check path exists
            if datadir and Path(datadir).exists():
                healthy += 1
            elif datadir:
                broken.append(f"{mount_point} → {datadir}")

            # Check not exposed to All
            if not applicable_groups and not applicable_users:
                exposed.append(mount_point)

        check("Mounts healthy",  healthy == total or broken == [],
              f"{healthy}/{total} paths exist on disk",
              warn=len(broken) > 0)

        if broken:
            for b in broken:
                check("  Missing path", False, b)

        check("Access control", len(exposed) == 0,
              "all scoped correctly" if not exposed else f"exposed to ALL: {', '.join(exposed)}",
              warn=len(exposed) > 0)

    except Exception as e:
        check("Mounts", False, f"error: {e}")

    # ── Disk space ────────────────────────────────────────────
    section("Disk Space")

    total, used, free = shutil.disk_usage("/mnt/data")
    pct  = (used / total) * 100
    free_gb = free / (1024**3)
    used_gb = used / (1024**3)
    total_gb = total / (1024**3)
    check("Data disk", pct < 90,
          f"{used_gb:.0f}GB used / {total_gb:.0f}GB total ({pct:.0f}%)",
          warn=80 <= pct < 90)

    total, used, free = shutil.disk_usage("/var/www/html/nextcloud")
    free_gb = free / (1024**3)
    check("System disk", free_gb > 5,
          f"{free_gb:.1f}GB free",
          warn=2 < free_gb <= 5)

    # ── Firewall ──────────────────────────────────────────────
    section("Security")

    code, out, _ = run(["ufw", "status"])
    check("Firewall", "active" in out.lower(), "active" if "active" in out.lower() else "INACTIVE")

    # Check fail2ban
    code, out, _ = run(["systemctl", "is-active", "fail2ban"])
    check("fail2ban", code == 0, "running" if code == 0 else "not running", warn=code != 0)

    # ── SSL ───────────────────────────────────────────────────
    section("SSL")

    code, out, _ = run(["certbot", "certificates"])
    if "No certificates found" in out or code != 0:
        check("SSL certificate", False, "none installed — HTTP only", warn=True)
    else:
        expiry_match = re.search(r"Expiry Date: (.+?) \(", out)
        if expiry_match:
            check("SSL certificate", True, f"expires {expiry_match.group(1)}")
        else:
            check("SSL certificate", True, "installed")

    # ── Agents ────────────────────────────────────────────────
    section("Agents")

    code, out, _ = run(["systemctl", "is-active", "nextcloud-refresh"])
    check("Refresh agent", code == 0,
          "running" if code == 0 else "not installed — run: systemctl enable nextcloud-refresh",
          warn=code != 0)

    log_path = Path("logs/refresh.log")
    if log_path.exists():
        age = (datetime.now().timestamp() - log_path.stat().st_mtime) / 60
        check("Last refresh", age < 5, f"{age:.0f} minutes ago", warn=age >= 5)

    # ── Summary ───────────────────────────────────────────────
    fails = [r for r in results if r["status"] == "FAIL"]
    warns = [r for r in results if r["status"] == "WARN"]
    oks   = [r for r in results if r["status"] == "OK"]

    print(f"\n{BOLD}{'═' * 50}")
    if not fails:
        print(f"  {GREEN}✅ ALL CHECKS PASSED{NC}  ({len(oks)} OK, {len(warns)} warnings)")
    else:
        print(f"  {RED}❌ {len(fails)} CHECKS FAILED{NC}  ({len(oks)} OK, {len(warns)} warnings)")
    print(f"{'═' * 50}{NC}\n")

    if args.json:
        print(json.dumps({
            "timestamp": ts(),
            "summary": {"ok": len(oks), "warn": len(warns), "fail": len(fails)},
            "checks": results
        }, indent=2))

    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
