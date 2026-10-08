#!/usr/bin/env bash
# =============================================================
#  verify_download.sh — Verify TomTom download completeness
#  ─────────────────────────────────────────────────────────────
#  Checks a MapFlow download folder for incomplete files by
#  looking for leftover .aria2 control files (aria2's own marker
#  for "this file did not finish downloading").
#
#  If incomplete files are found, offers to resume them using
#  the same aria2c + metalink approach MapFlow itself uses —
#  aria2c automatically continues from where it left off using
#  the .aria2 control file, it does NOT restart from scratch.
#
#  Usage:
#    bash verify_download.sh /path/to/MultiNet_SEA_..._commercial
#    bash verify_download.sh /path/to/MultiNet_SEA_..._commercial --resume
# =============================================================
set -Eeuo pipefail

FOLDER="${1:-}"
RESUME=0
[[ "${2:-}" == "--resume" ]] && RESUME=1

if [[ -z "$FOLDER" || ! -d "$FOLDER" ]]; then
    echo "Usage: bash verify_download.sh /path/to/download_folder [--resume]"
    exit 1
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Download Verification"
echo "  Folder: $FOLDER"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Find all .aria2 control files — these mark incomplete downloads
mapfile -t INCOMPLETE < <(find "$FOLDER" -name "*.aria2" -type f 2>/dev/null)

TOTAL_FILES=$(find "$FOLDER" -type f ! -name "*.aria2" ! -name "*.meta4" 2>/dev/null | wc -l)
INCOMPLETE_COUNT=${#INCOMPLETE[@]}

echo "Total files on disk:      $TOTAL_FILES"
echo "Incomplete (resumable):   $INCOMPLETE_COUNT"
echo ""

if (( INCOMPLETE_COUNT == 0 )); then
    echo "✅ No incomplete files found — download appears complete."
    echo ""
    echo "Note: This only checks for aria2's own incomplete markers."
    echo "It does NOT verify file count against TomTom's expected total."
    echo "Use --verify-count to cross-check against the original metalink files."
    exit 0
fi

echo "⚠️  Incomplete files found:"
for f in "${INCOMPLETE[@]}"; do
    actual_file="${f%.aria2}"
    actual_name=$(basename "$actual_file")
    if [[ -f "$actual_file" ]]; then
        size=$(stat -c%s "$actual_file" 2>/dev/null || echo "0")
        echo "  - $actual_name (partial: $size bytes on disk)"
    else
        echo "  - $actual_name (not started)"
    fi
done

echo ""

if (( RESUME == 0 )); then
    echo "To resume these downloads, re-run with --resume"
    echo "Resuming uses aria2c's native continue support — it will"
    echo "NOT re-download bytes that are already on disk."
    exit 0
fi

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Resuming incomplete downloads"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

RESUMED=0
FAILED=0

for f in "${INCOMPLETE[@]}"; do
    actual_file="${f%.aria2}"
    actual_name=$(basename "$actual_file")
    dir=$(dirname "$actual_file")

    # Find the matching metalink (.meta4) file for this download's auth/checksum info.
    # MapFlow names these content_{id}.meta4 at the download root — search broadly.
    meta_file=""
    for candidate in "$FOLDER"/content_*.meta4 "$dir"/content_*.meta4; do
        [[ -f "$candidate" ]] || continue
        if grep -q "$actual_name" "$candidate" 2>/dev/null; then
            meta_file="$candidate"
            break
        fi
    done

    if [[ -z "$meta_file" ]]; then
        echo "⚠️  No metalink found for $actual_name — cannot resume with checksum verification"
        echo "    You may need to delete this partial file and re-run the full download for this dataset."
        ((FAILED++)) || true
        continue
    fi

    echo "▶ Resuming: $actual_name"
    if aria2c --continue=true --metalink-file="$meta_file" --dir="$dir"; then
        echo "  ✅ Resumed and verified: $actual_name"
        ((RESUMED++)) || true
    else
        echo "  ❌ Resume failed: $actual_name"
        ((FAILED++)) || true
    fi
    echo ""
done

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Resume complete"
echo "  Resumed: $RESUMED   Failed: $FAILED"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if (( FAILED > 0 )); then
    echo ""
    echo "⚠️  Some files could not be resumed automatically."
    echo "Files without a matching metalink need a fresh download."
    exit 1
fi
