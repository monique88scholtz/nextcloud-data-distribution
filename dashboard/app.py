#!/usr/bin/env python3
"""
dashboard/app.py — GeoInt Distribution Dashboard v2
Enhanced with:
  - Auto-detects existing quarter folder structures
  - Suggests next quarter automatically
  - Detects active Mapflow downloads in progress
  - Config editor UI
"""

import json
import os
import re
import subprocess
import threading
from datetime import datetime
from pathlib import Path

import yaml
from dotenv import dotenv_values
from flask import Flask, jsonify, render_template, request

app = Flask(__name__, template_folder="templates", static_folder="static")

BASE_DIR   = Path(__file__).parent.parent
SCRIPTS    = BASE_DIR / "scripts"
AGENTS     = BASE_DIR / "agents"
QUARTERLY  = BASE_DIR / "quarterly"
DATA_DIR   = BASE_DIR / "data"
CONFIG_ENV = BASE_DIR / "config" / ".env"
FOLDERS_ENV= BASE_DIR / ".folders.env"
MAPFLOW    = Path("/mnt/data/MapFlow/mapflow")
LOGS_DIR   = Path("/mnt/data/MapFlow/logs")
DOWNLOADS  = Path("/mnt/data/downloads")
DISTRO_BASE= Path("/mnt/data")
NC_OCC     = Path("/var/www/html/nextcloud/occ")

LOGS_DIR.mkdir(parents=True, exist_ok=True)
running_jobs = {}


# ── Quarter detection ─────────────────────────────────────────
def get_all_quarters():
    """Find all DISTRIBUTION_* folders on disk + YAML configs."""
    quarters = set()
    for d in DISTRO_BASE.glob("DISTRIBUTION_*"):
        if d.is_dir():
            quarters.add(d.name.replace("DISTRIBUTION_", ""))
    for f in QUARTERLY.glob("*.yaml"):
        quarters.add(f.stem)
    return sorted(quarters, reverse=True)


def suggest_next_quarter(existing):
    """Given existing quarters, suggest the next one."""
    if not existing:
        now = datetime.now()
        q = (now.month - 1) // 3 + 1
        return f"Q{q}_{now.year}"
    latest = sorted(existing)[-1]
    m = re.match(r"Q(\d)_(\d{4})", latest)
    if not m:
        return None
    q, y = int(m.group(1)), int(m.group(2))
    q += 1
    if q > 4:
        q = 1
        y += 1
    return f"Q{q}_{y}"


def get_quarter_status(quarter):
    """Return detailed status of a quarter folder."""
    path = DISTRO_BASE / f"DISTRIBUTION_{quarter}"
    if not path.exists():
        return {"exists": False, "folders": 0, "size": "0", "products": []}
    folders = [d.name for d in path.iterdir() if d.is_dir()]
    try:
        r = subprocess.run(["du", "-sh", str(path)], capture_output=True, text=True)
        size = r.stdout.split()[0] if r.stdout else "?"
    except:
        size = "?"
    return {"exists": True, "folders": len(folders), "size": size, "products": folders}


# ── Mapflow download detection ────────────────────────────────
def get_mapflow_status():
    """Detect if Mapflow is currently downloading and what."""
    status = {"running": False, "processes": [], "recent_logs": [], "downloads": []}

    try:
        r = subprocess.run(
            ["pgrep", "-a", "-f", "mapflow.main"],
            capture_output=True, text=True)
        if r.stdout.strip():
            status["running"] = True
            for line in r.stdout.strip().split("\n"):
                if line:
                    parts = line.split(None, 1)
                    status["processes"].append({
                        "pid": parts[0],
                        "cmd": parts[1] if len(parts) > 1 else ""
                    })
    except:
        pass

    try:
        log_files = sorted(LOGS_DIR.glob("download_*.log"), key=lambda f: f.stat().st_mtime, reverse=True)
        for lf in log_files[:3]:
            lines = lf.read_text().strip().split("\n")
            last_lines = [l for l in lines[-20:] if l.strip()]
            age_secs = datetime.now().timestamp() - lf.stat().st_mtime
            status["recent_logs"].append({
                "name": lf.name,
                "last_lines": last_lines,
                "age_mins": round(age_secs / 60),
                "active": age_secs < 300
            })
    except:
        pass

    try:
        for d in sorted(DOWNLOADS.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True)[:10]:
            if d.is_dir():
                try:
                    r = subprocess.run(["du", "-sh", str(d)], capture_output=True, text=True, timeout=5)
                    size = r.stdout.split()[0] if r.stdout else "?"
                except:
                    size = "?"
                age_secs = datetime.now().timestamp() - d.stat().st_mtime
                status["downloads"].append({
                    "name": d.name,
                    "size": size,
                    "age_mins": round(age_secs / 60),
                    "active": age_secs < 600
                })
    except:
        pass

    return status


# ── Config helpers ────────────────────────────────────────────
def read_env_file(path):
    """Read an env file into key/value pairs preserving comments."""
    result = []
    try:
        for line in Path(path).read_text().split("\n"):
            stripped = line.strip()
            if stripped.startswith("#") or stripped == "":
                result.append({"type": "comment", "raw": line})
            elif "=" in stripped:
                key, _, val = stripped.partition("=")
                result.append({"type": "var", "key": key.strip(), "value": val.strip(), "raw": line})
            else:
                result.append({"type": "comment", "raw": line})
    except:
        pass
    return result


def write_env_file(path, entries):
    """Write entries back to env file."""
    lines = []
    for e in entries:
        if e["type"] == "comment":
            lines.append(e["raw"])
        else:
            lines.append(f"{e['key']}={e['value']}")
    Path(path).write_text("\n".join(lines) + "\n")


# ── System status ─────────────────────────────────────────────
def get_system_status():
    status = {}
    for svc in ["apache2", "mariadb", "redis-server", "nextcloud-refresh"]:
        try:
            r = subprocess.run(["systemctl", "is-active", svc], capture_output=True, text=True)
            status[svc.replace("-", "_")] = r.stdout.strip() == "active"
        except:
            status[svc.replace("-", "_")] = False

    try:
        r = subprocess.run(
            ["sudo", "-u", "www-data", "php", str(NC_OCC), "status", "--output=json"],
            capture_output=True, text=True, timeout=10)
        nc = json.loads(r.stdout)
        status["nextcloud"] = nc.get("installed", False)
        status["nc_version"] = nc.get("versionstring", "?")
        status["nc_maintenance"] = nc.get("maintenance", False)
    except:
        status["nextcloud"] = False
        status["nc_version"] = "?"
        status["nc_maintenance"] = False

    try:
        r = subprocess.run(["df", "-h", "/mnt/data"], capture_output=True, text=True)
        parts = r.stdout.strip().split("\n")[1].split()
        status["disk_used"]  = parts[2]
        status["disk_avail"] = parts[3]
        status["disk_pct"]   = parts[4]
    except:
        status["disk_used"] = status["disk_avail"] = status["disk_pct"] = "?"

    try:
        r = subprocess.run(
            ["sudo", "-u", "www-data", "php", str(NC_OCC), "user:list", "--output=json"],
            capture_output=True, text=True, timeout=10)
        users = json.loads(r.stdout)
        status["client_count"] = len([u for u in users if u != "admin"])
    except:
        status["client_count"] = "?"

    return status


# ── Job runner ────────────────────────────────────────────────
def stream_command(cmd, job_id, env=None, cwd=None):
    running_jobs[job_id] = {"status": "running", "output": [], "started": datetime.now().isoformat()}
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=full_env, cwd=str(cwd) if cwd else None)
        for line in proc.stdout:
            running_jobs[job_id]["output"].append(line.rstrip())
        proc.wait()
        running_jobs[job_id]["status"] = "done" if proc.returncode == 0 else "error"
        running_jobs[job_id]["returncode"] = proc.returncode
    except Exception as e:
        running_jobs[job_id]["output"].append(f"ERROR: {e}")
        running_jobs[job_id]["status"] = "error"


def run_bg(cmd, job_id, env=None, cwd=None):
    t = threading.Thread(target=stream_command, args=(cmd, job_id, env, cwd), daemon=True)
    t.start()
    return job_id


# ── Routes ────────────────────────────────────────────────────
@app.route("/")
def index():
    quarters    = get_all_quarters()
    next_q      = suggest_next_quarter(quarters)
    mapflow     = get_mapflow_status()
    status      = get_system_status()
    return render_template("index.html",
        quarters=quarters, next_quarter=next_q,
        mapflow=mapflow, status=status)


@app.route("/api/status")
def api_status():
    return jsonify(get_system_status())


@app.route("/api/quarters")
def api_quarters():
    quarters = get_all_quarters()
    next_q   = suggest_next_quarter(quarters)
    details  = {q: get_quarter_status(q) for q in quarters}
    return jsonify({"quarters": quarters, "next": next_q, "details": details})


@app.route("/api/mapflow")
def api_mapflow():
    return jsonify(get_mapflow_status())


@app.route("/api/job/<job_id>")
def job_status(job_id):
    return jsonify(running_jobs.get(job_id, {"status": "not_found"}))


# ── Config API ────────────────────────────────────────────────
@app.route("/api/config/env", methods=["GET"])
def get_config_env():
    return jsonify(read_env_file(CONFIG_ENV))


@app.route("/api/config/env", methods=["POST"])
def save_config_env():
    entries = request.json.get("entries", [])
    write_env_file(CONFIG_ENV, entries)
    return jsonify({"ok": True})


@app.route("/api/config/folders", methods=["GET"])
def get_config_folders():
    return jsonify(read_env_file(FOLDERS_ENV))


@app.route("/api/config/folders", methods=["POST"])
def save_config_folders():
    entries = request.json.get("entries", [])
    write_env_file(FOLDERS_ENV, entries)
    return jsonify({"ok": True})


@app.route("/api/config/quarterly/<quarter>", methods=["GET"])
def get_quarterly(quarter):
    path = QUARTERLY / f"{quarter}.yaml"
    if not path.exists():
        return jsonify({"error": "not found"}), 404
    return jsonify({"content": path.read_text()})


@app.route("/api/config/quarterly/<quarter>", methods=["POST"])
def save_quarterly(quarter):
    content = request.json.get("content", "")
    path = QUARTERLY / f"{quarter}.yaml"
    QUARTERLY.mkdir(exist_ok=True)
    path.write_text(content)
    return jsonify({"ok": True})


# ── Pipeline actions ──────────────────────────────────────────
@app.route("/api/run/healthcheck", methods=["POST"])
def run_healthcheck():
    job_id = f"health_{datetime.now().strftime('%H%M%S')}"
    run_bg(["python3", str(SCRIPTS / "healthcheck.py"), "--env", str(CONFIG_ENV)], job_id)
    return jsonify({"job_id": job_id})


@app.route("/api/run/create_structure", methods=["POST"])
def run_create_structure():
    quarter = request.json.get("quarter", "Q1_2026")
    job_id  = f"structure_{datetime.now().strftime('%H%M%S')}"
    run_bg(["bash", str(SCRIPTS / "create_structure.sh"), "--quarter", quarter], job_id)
    return jsonify({"job_id": job_id})


@app.route("/api/run/download", methods=["POST"])
def run_download():
    family  = request.json.get("family", "MNR")
    quarter = request.json.get("quarter", "Q1_2026")
    job_id  = f"download_{family}_{datetime.now().strftime('%H%M%S')}"
    log_file = LOGS_DIR / f"download_{family}_{quarter}.log"
    run_bg(["bash", "-c", f"bash {MAPFLOW}/download.sh 2>&1 | tee {log_file}"],
           job_id, env={"MAPFLOW_FAMILY": family, "MAPFLOW_QUARTER": quarter})
    return jsonify({"job_id": job_id})


@app.route("/api/run/download_mapit", methods=["POST"])
def run_download_mapit():
    quarter  = request.json.get("quarter", "Q1_2026")
    job_id   = f"mapit_{datetime.now().strftime('%H%M%S')}"
    log_file = LOGS_DIR / f"download_mapit_{quarter}.log"
    run_bg(["bash", "-c", f"bash {MAPFLOW}/download_mapit.sh 2>&1 | tee {log_file}"], job_id)
    return jsonify({"job_id": job_id})


@app.route("/api/run/ingest", methods=["POST"])
def run_ingest():
    dry_run  = request.json.get("dry_run", False)
    quarter  = request.json.get("quarter", "Q1_2026")
    job_id   = f"ingest_{datetime.now().strftime('%H%M%S')}"
    log_file = LOGS_DIR / f"ingest_{quarter}.log"
    cmd = ["bash", str(SCRIPTS / "ingest.sh"),
           "--env", str(FOLDERS_ENV), "--log", str(log_file)]
    if dry_run:
        cmd.append("--dry-run")
    run_bg(cmd, job_id)
    return jsonify({"job_id": job_id})


@app.route("/api/run/provision", methods=["POST"])
def run_provision():
    dry_run = request.json.get("dry_run", False)
    job_id  = f"provision_{datetime.now().strftime('%H%M%S')}"
    cmd = ["python3", str(SCRIPTS / "provision_clients.py"),
           "--env", str(CONFIG_ENV),
           "--csv", str(DATA_DIR / "client_distribution.csv"), "--report"]
    if dry_run:
        cmd.append("--dry-run")
    run_bg(cmd, job_id)
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
    run_bg(["sudo", "-u", "www-data", "php", str(NC_OCC), "files:scan", "--all"], job_id)
    return jsonify({"job_id": job_id})


@app.route("/api/run/backup", methods=["POST"])
def run_backup():
    job_id = f"backup_{datetime.now().strftime('%H%M%S')}"
    run_bg(["bash", str(SCRIPTS / "backup.sh")], job_id)
    return jsonify({"job_id": job_id})


@app.route("/api/clients")
def get_clients():
    try:
        r = subprocess.run(
            ["sudo", "-u", "www-data", "php", str(NC_OCC), "user:list", "--output=json"],
            capture_output=True, text=True, timeout=10)
        users = json.loads(r.stdout)
        return jsonify([{"username": k, "display": v} for k, v in users.items() if k != "admin"])
    except:
        return jsonify([])


if __name__ == "__main__":
    print("\n" + "="*52)
    print("  GeoInt Distribution Dashboard v2")
    print("  http://0.0.0.0:5050")
    print("="*52 + "\n")
    app.run(host="0.0.0.0", port=5050, debug=False)
