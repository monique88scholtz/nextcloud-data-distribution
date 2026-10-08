#!/usr/bin/env bash
# record_push_status.sh — record a successful push to push_status.json
# Usage: bash record_push_status.sh CLIENT_ID DATASET REMOTE_BASE LOCAL_PATH
CLIENT_ID="${1:-}"
DATASET="${2:-}"
REMOTE_BASE="${3:-}"
LOCAL_PATH="${4:-}"

[[ -z "$CLIENT_ID" || -z "$DATASET" ]] && exit 0

STATUS_FILE="/opt/nextcloud-setup/nextcloud-data-distribution/data/push_status.json"
mkdir -p "$(dirname "$STATUS_FILE")"

FILE_COUNT=$(find "$LOCAL_PATH" -type f 2>/dev/null | wc -l)
FOLDER_NAME=$(basename "$REMOTE_BASE")
PUSHED_AT=$(date -Iseconds)

python3 - << PYEOF
import json
from pathlib import Path

sf = Path("$STATUS_FILE")
data = {}
if sf.exists():
    try:
        data = json.loads(sf.read_text())
    except Exception:
        data = {}

data["${CLIENT_ID}_${DATASET}"] = {
    "client_id": "$CLIENT_ID",
    "dataset": "$DATASET",
    "folder_name": "$FOLDER_NAME",
    "pushed_at": "$PUSHED_AT",
    "file_count": $FILE_COUNT,
}
sf.write_text(json.dumps(data, indent=2))
PYEOF
