#!/usr/bin/env bash
set -euo pipefail

Q="${1:-Q2_2026}"
DISTRO="/mnt/data/DISTRIBUTION_${Q}"
MNR_ROOT="${DISTRO}/MNR"

SRC="${MNR_ROOT}/MNR_MEA"
ZAF_DEST="${MNR_ROOT}/MNR_ZAF"
SA_DEST="${MNR_ROOT}/MNR_SOUTHERN_AFRICA"

SA_COUNTRIES=(ago bwa lso moz mwi nam swz zaf zmb zwe)

if [[ ! -d "$SRC" ]]; then
  echo "ERROR: Source folder not found: $SRC"
  exit 1
fi

if ! command -v rsync >/dev/null 2>&1; then
  echo "Installing rsync..."
  apt update
  apt install -y rsync
fi

find_country_dir() {
  local cc="$1"
  local upper
  upper="$(echo "$cc" | tr '[:lower:]' '[:upper:]')"

  local candidates=(
    "$SRC/$cc"
    "$SRC/$upper"
    "$SRC/data/mea/$cc"
    "$SRC/data/mea/$upper"
    "$SRC/mea/$cc"
    "$SRC/mea/$upper"
  )

  for p in "${candidates[@]}"; do
    if [[ -d "$p" ]]; then
      echo "$p"
      return 0
    fi
  done

  return 1
}

copy_country() {
  local cc="$1"
  local dest="$2"
  local src_country

  if ! src_country="$(find_country_dir "$cc")"; then
    echo "WARN: Country folder not found in MNR_MEA for: $cc"
    return 0
  fi

  echo "Copying $cc:"
  echo "  from: $src_country"
  echo "  to:   $dest/$(basename "$src_country")"

  if [[ "${RUN:-0}" == "1" ]]; then
    rsync -a --info=progress2 "$src_country" "$dest/"
  else
    rsync -an --info=stats1 "$src_country" "$dest/"
  fi
}

echo "Quarter: $Q"
echo "Source:  $SRC"
echo

if [[ "${RUN:-0}" == "1" ]]; then
  echo "RUN=1 set — applying changes."
  mkdir -p "$ZAF_DEST" "$SA_DEST"

  echo "Cleaning existing subset folders first..."
  rm -rf "${ZAF_DEST:?}/"*
  rm -rf "${SA_DEST:?}/"*
else
  echo "Preview mode only. No files will be copied."
  echo "After checking the output, run again with:"
  echo "RUN=1 bash /opt/nextcloud-setup/nextcloud-data-distribution/scripts/recover_mnr_subsets_from_mea.sh $Q"
  echo
fi

echo "=== Recovering MNR_ZAF ==="
copy_country zaf "$ZAF_DEST"

echo
echo "=== Recovering MNR_SOUTHERN_AFRICA ==="
for cc in "${SA_COUNTRIES[@]}"; do
  copy_country "$cc" "$SA_DEST"
done

echo
echo "=== Verification ==="
for d in "$ZAF_DEST" "$SA_DEST"; do
  echo
  echo "$d"
  if [[ -d "$d" ]]; then
    du -sh "$d" 2>/dev/null || true
    echo "tar.gz files: $(find "$d" -type f -name '*.tar.gz' 2>/dev/null | wc -l)"
    echo "top-level folders:"
    find "$d" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null | sort | tr '\n' ' '
    echo
  else
    echo "missing"
  fi
done
