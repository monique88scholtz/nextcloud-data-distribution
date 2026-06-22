#!/usr/bin/env bash
# =============================================================
#  ingest.sh — MASTER DATA INGESTION PIPELINE
#  ─────────────────────────────────────────────────────────────
#  Single entry point for the full quarterly ingestion process:
#    1. Pre-flight checks (disk space, source paths, permissions)
#    2. Build distribution folder structure
#    3. Copy files preserving TomTom's module subfolder structure
#    4. Copy region documentation (model, loader, psp_library) to
#       MNR_DOCUMENTATION_{region}/
#    5. Tidy MN files into country subfolders
#    6. QC check — verify MNR module structure is correct
#    7. Trigger Nextcloud file scan
#    8. Print summary report
#
#  TomTom MNR download structure:
#    MultiNet-R_MEA_2026.06.003_full_commercial/
#    └── data/
#        ├── model.tar.gz        ← region-level, goes to MNR_DOCUMENTATION_MEA/
#        ├── loader.tar.gz       ← region-level, goes to MNR_DOCUMENTATION_MEA/
#        ├── psp_library.tar.gz  ← region-level, goes to MNR_DOCUMENTATION_MEA/
#        ├── documentation.tar.gz
#        └── mea/
#            └── {country}/
#                └── {country}/  ← double country folder (TomTom standard)
#                    └── {module}/
#                        └── {country}_{module}.tar.gz
#
#  The ingest script mirrors this structure exactly:
#    MNR_MEA/{country}/{module}/{country}_{module}.tar.gz
#
#  Usage:
#    sudo bash ingest.sh --env .folders.env --dry-run
#    sudo bash ingest.sh --env .folders.env
#    sudo bash ingest.sh --env .folders.env --only MNR_MEA,MNR_EUR
#    sudo bash ingest.sh --env .folders.env --skip SP_MEA --log /mnt/data/logs/ingest.log
#
#  Safe to re-run — skips files that already exist at same size.
# =============================================================
set -Eeuo pipefail
IFS=$'\n\t'

# ── Defaults ──────────────────────────────────────────────────
ENV_FILE="./.folders.env"
DRY_RUN=0
MODE="copy"
ONLY=""
SKIP=""
LOG_FILE=""
SKIP_TIDY=0
SKIP_CLEANUP=0
SKIP_NEXTCLOUD=0
SKIP_QC=0
NC_PATH="/var/www/html/nextcloud"

# ── Valid MNR module names ─────────────────────────────────────
VALID_MNR_MODULES=(ada apt buildings core generic geopolitical geospatial junction_views level_0 logistics phonemes poi premium_speed_profiles speed_profiles traffic_signs)

# ── Region documentation files ─────────────────────────────────
# These sit at data/{region}/ level — one set per region download
REGION_DOC_FILES="model.tar.gz loader.tar.gz psp_library.tar.gz documentation.tar.gz version.csv"

# ── Colours ───────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC_COL='\033[0m'
log()     { echo -e "[$(date '+%F %T')] $*"; }
ok()      { echo -e "[$(date '+%F %T')] ${GREEN}✅${NC_COL} $*"; }
warn()    { echo -e "[$(date '+%F %T')] ${YELLOW}⚠️ ${NC_COL} $*"; }
die()     { echo -e "[$(date '+%F %T')] ${RED}❌${NC_COL} $*"; exit 1; }
section() { echo -e "\n${BOLD}${CYAN}━━━  $*  ━━━${NC_COL}\n"; }
run() {
    if (( DRY_RUN )); then log "DRYRUN: $*"; return 0; fi
    log "RUN: $*"; "$@"
}
on_err() { local c=$?; log "${RED}ERROR (exit=$c) at line $1: $2${NC_COL}"; exit "$c"; }
trap 'on_err "$LINENO" "$BASH_COMMAND"' ERR

# ── Helpers ───────────────────────────────────────────────────
csv_has() {
    local csv="$1" key="$2"
    [[ -z "$csv" ]] && return 1
    IFS=',' read -r -a arr <<< "$csv"
    for x in "${arr[@]}"; do [[ "${x//[[:space:]]/}" == "$key" ]] && return 0; done
    return 1
}
should_run() {
    local key="$1"
    [[ -n "$ONLY" ]] && ! csv_has "$ONLY" "$key" && return 1
    [[ -n "$SKIP" ]] && csv_has "$SKIP" "$key" && return 1
    return 0
}
resolve_source() {
    local v="${1//\"/}"; v="${v//\'/}"
    [[ "$v" == /* ]] && echo "$v" || echo "$DATA_FOLDER/$v"
}
need_cmd() { command -v "$1" >/dev/null 2>&1 || die "Missing command: $1 (run: apt install $1)"; }

mn_country_from_filename() {
    local f="$1"
    local cc
    cc=$(echo "$f" | cut -d'-' -f4 | tr '[:upper:]' '[:lower:]')
    if [[ -n "$cc" && ${#cc} -le 3 && "$cc" =~ ^[a-z]{2,3}$ ]]; then
        echo "$cc"
        return
    fi
    echo ""
}

# ── File copy with atomic write + size verification ───────────
COPIED=0; SKIPPED=0; REPLACED=0; ERRORS=0
copy_file() {
    local src="$1" dest="$2"
    [[ -f "$src" ]] || return 0
    run mkdir -p "$(dirname "$dest")"
    local src_size
    src_size=$(stat -c%s "$src")
    if [[ -f "$dest" ]]; then
        local dest_size
        dest_size=$(stat -c%s "$dest")
        if (( src_size == dest_size )); then
            log "  ⏩ Same size, skip: $(basename "$dest")"
            (( SKIPPED++ )) || true
            [[ "$MODE" == "move" && "${PRESERVE_SOURCE:-0}" != "1" ]] && run rm -f "$src" || true
            return 0
        fi
        if (( src_size > dest_size )); then
            log "  🔁 Replacing (src bigger): $(basename "$dest")"
        else
            log "  ⏩ Dest bigger, skip: $(basename "$dest")"
            (( SKIPPED++ )) || true
            return 0
        fi
    fi
    local tmp
    tmp="$(dirname "$dest")/.tmp.$(basename "$dest").$$"
    run rsync -a --no-perms --no-owner --no-group "$src" "$tmp"
    run mv -f "$tmp" "$dest"
    if (( ! DRY_RUN )); then
        local new_size
        new_size=$(stat -c%s "$dest")
        if (( new_size != src_size )); then
            warn "  Size mismatch after copy! src=$src_size dest=$new_size"
            (( ERRORS++ )) || true
            return 0
        fi
    fi
    ok "  Copied: $(basename "$src")"
    (( COPIED++ )) || true
    [[ "$MODE" == "move" && "${PRESERVE_SOURCE:-0}" != "1" ]] && { run rm -f "$src"; log "  🧹 Removed source"; } || true
}

has_any_file() {
    local p="$1"
    [[ -d "$p" ]] || return 1
    find "$p" -type f -print -quit 2>/dev/null | grep -q .
}

# ── QC: Verify MNR module structure ───────────────────────────
qc_mnr() {
    local mnr_root="$1"
    [[ -d "$mnr_root" ]] || return 0
    local errors=0 warnings=0 checked=0
    log "🔍 QC: Checking MNR structure in $(basename "$mnr_root")"

    for country_dir in "$mnr_root"/*/; do
        [[ -d "$country_dir" ]] || continue
        local cc
        cc=$(basename "$country_dir")
        ((checked++)) || true

        # Check for loose tar.gz files not inside a module subfolder
        while IFS= read -r -d '' loose; do
            warn "  ❌ QC FAIL: Loose file not in module subfolder: $cc/$(basename "$loose")"
            ((errors++)) || true
        done < <(find "$country_dir" -maxdepth 1 -name "*.tar.gz" -type f -print0 2>/dev/null)

        # Check for invalid module folder names
        for module_dir in "$country_dir"*/; do
            [[ -d "$module_dir" ]] || continue
            local mod
            mod=$(basename "$module_dir")
            local valid=0
            for vm in "${VALID_MNR_MODULES[@]}"; do
                [[ "$mod" == "$vm" ]] && { valid=1; break; }
            done
            if (( ! valid )); then
                warn "  ⚠️  QC WARN: Invalid module name '$mod' in $cc"
                ((warnings++)) || true
            fi
        done

        # Warn if core is missing (every country should have core)
        if [[ ! -d "$country_dir/core" ]]; then
            warn "  ⚠️  QC WARN: $cc is missing core/ module"
            ((warnings++)) || true
        fi
    done

    if (( errors == 0 && warnings == 0 )); then
        ok "QC passed: $(basename "$mnr_root") — $checked countries checked"
    else
        warn "QC complete: $errors errors, $warnings warnings in $(basename "$mnr_root")"
    fi
    return $errors
}

# ── Process MNR dataset ────────────────────────────────────────
process_mnr() {
    local key="$1" src_base="$2" dest_base="$3" docs_dest="$4"
    local zones_filter="${5:-}"
    local fallback_base="${6:-}"

    if [[ -n "$zones_filter" ]]; then
        PRESERVE_SOURCE=1
    else
        PRESERVE_SOURCE=0
    fi

    log "────────────────────────────────────"
    log "📦 MultiNet-R ($key)"
    log "   src : $src_base"
    log "   dest: $dest_base"
    log "   docs: $docs_dest"
    log "   mode: $MODE | zones: ${zones_filter:-all} | dry-run: $DRY_RUN"
    log "────────────────────────────────────"

    # Fallback if source already moved into parent
    if ! has_any_file "$src_base"; then
        if [[ -n "$zones_filter" && -n "$fallback_base" ]] && has_any_file "$fallback_base"; then
            warn "Source missing — using already-organised parent: $fallback_base"
            src_base="$fallback_base"
        elif has_any_file "$dest_base"; then
            ok "Already ingested: $key"
            return 0
        else
            warn "Source missing, skipping: $src_base"
            return 0
        fi
    fi

    run mkdir -p "$dest_base" "$docs_dest"

    # ── Region-level documentation files ──────────────────────
    # These sit at src_base/ level (e.g. data/mea/model.tar.gz)
    # Copy each to MNR_DOCUMENTATION_{region}/
    log "  📄 Checking for region documentation files..."
    for _doc_file in $REGION_DOC_FILES; do
        local _doc_src="$src_base/$_doc_file"
        if [[ -f "$_doc_src" ]]; then
            log "  📋 Copying $_doc_file → $(basename "$docs_dest")/"
            copy_file "$_doc_src" "$docs_dest/$_doc_file"
        fi
    done

    # Also check for documentation/ and tools/ subfolders
    for _dir_src in "$src_base/documentation" "$src_base/tools"; do
        if [[ -d "$_dir_src" ]]; then
            log "  📁 Copying $(basename "$_dir_src")/ folder → $docs_dest/"
            run rsync -a --no-perms --no-owner --no-group "$_dir_src/" "$docs_dest/$(basename "$_dir_src")/"
        fi
    done

    # ── Discover country folders ───────────────────────────────
    mapfile -t all_countries < <(
        find "$src_base" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' \
          | grep -v '^documentation$' \
          | grep -v '^tools$' \
          | grep -v '^[[:space:]]*$' \
          | sort -u
    )
    mapfile -t all_countries < <(printf "%s\n" "${all_countries[@]}" | grep -v "^$")

    local countries=()
    if [[ -n "$zones_filter" ]]; then
        IFS=',' read -r -a allowed_zones <<< "$zones_filter"
        for cc in "${all_countries[@]}"; do
            for zone in "${allowed_zones[@]}"; do
                if [[ "${cc,,}" == "${zone,,}" ]]; then
                    countries+=("$cc")
                    break
                fi
            done
        done
        log "  🗺️  Zone filter: ${zones_filter} → ${#countries[@]} of ${#all_countries[@]} countries"
    else
        countries=("${all_countries[@]}")
    fi

    if (( ${#countries[@]} == 0 )); then
        warn "No country folders found in $src_base"
        return 0
    fi

    log "  🌍 Processing ${#countries[@]} country folder(s)"

    for cc in "${countries[@]}"; do
        local country_src="$src_base/$cc"
        [[ -d "$country_src" ]] || { warn "Country folder not found: $country_src"; continue; }

        # TomTom uses a double country folder: data/mea/{cc}/{cc}/{module}/
        # Detect and use the inner folder if it exists
        local inner_src="$country_src/$cc"
        if [[ -d "$inner_src" ]]; then
            country_src="$inner_src"
            log "  🔍 Using inner country folder: $cc/$cc/"
        fi

        # Copy all files preserving module subfolder structure
        while IFS= read -r -d '' file; do
            local fname rel
            fname="$(basename "$file")"
            rel="${file#"$country_src/"}"

            # Skip region-level doc files if they ended up here
            local is_doc=0
            for _df in $REGION_DOC_FILES; do
                [[ "$fname" == "$_df" ]] && { is_doc=1; break; }
            done
            (( is_doc )) && continue

            # Preserve the exact relative path (module subfolder included)
            # e.g. apt/are_apt.tar.gz → dest/are/apt/are_apt.tar.gz
            copy_file "$file" "$dest_base/$cc/$rel"

        done < <(find "$country_src" -type f -print0 2>/dev/null)
    done

    ok "Finished: MultiNet-R ($key)"
}

# ── Process MN/SP/POI flat dataset ────────────────────────────
process_flat() {
    local key="$1" type="$2" src_base="$3" dest_base="$4" docs_dest="$5"
    local zones_filter="${6:-}"
    local fallback_base="${7:-}"

    if [[ -n "$zones_filter" ]]; then
        PRESERVE_SOURCE=1
    else
        PRESERVE_SOURCE=0
    fi

    log "────────────────────────────────────"
    log "📦 $type ($key)"
    log "   src : $src_base"
    log "   dest: $dest_base"
    log "   mode: $MODE | zones: ${zones_filter:-all} | dry-run: $DRY_RUN"
    log "────────────────────────────────────"

    # Fallback handling
    if ! has_any_file "$src_base"; then
        if [[ -n "$zones_filter" && -n "$fallback_base" ]] && has_any_file "$fallback_base"; then
            warn "Source missing — using fallback: $fallback_base"
            src_base="$fallback_base"
            PRESERVE_SOURCE=1
        elif has_any_file "$dest_base"; then
            ok "Already ingested: $key"
            return 0
        else
            warn "Source missing, skipping: $src_base"
            return 0
        fi
    fi

    run mkdir -p "$dest_base" "$docs_dest"

    # Copy documentation folder if present
    for _doc_src in "$src_base/documentation" "$(dirname "$src_base")/documentation"; do
        if [[ -d "$_doc_src" ]]; then
            log "  📄 Copying documentation from: $_doc_src"
            run rsync -a --no-perms --no-owner --no-group "$_doc_src/" "$docs_dest/"
            break
        fi
    done

    log "  ➡️  Flat mode — sorting into country folders by filename"
    while IFS= read -r -d '' file; do
        local fname cc
        fname="$(basename "$file")"
        cc="$(mn_country_from_filename "$fname")"

        # Apply zone filter
        if [[ -n "$zones_filter" && -n "$cc" ]]; then
            IFS=',' read -r -a _zones <<< "$zones_filter"
            local _match=0
            for _z in "${_zones[@]}"; do
                [[ "${cc,,}" == "${_z,,}" ]] && { _match=1; break; }
            done
            (( _match )) || continue
        fi

        if [[ -n "$cc" ]]; then
            copy_file "$file" "$dest_base/$cc/$fname"
        else
            copy_file "$file" "$dest_base/$fname"
        fi
    done < <(find "$src_base" -maxdepth 2 -type f -name "*.7z.001" -print0 2>/dev/null)

    ok "Finished: $type ($key)"
}

# ── Tidy MN root-level files into country subfolders ──────────
tidy_mn() {
    local root="$1"
    [[ -d "$root" ]] || { warn "Tidy: missing root $root"; return 0; }
    log "🧹 Tidying MN root: $root"
    local moved=0 deleted=0 warned=0
    while IFS= read -r -d '' f; do
        local base cc cc_dir dest
        base="$(basename "$f")"
        cc="$(mn_country_from_filename "$base")"
        if [[ -z "$cc" ]]; then
            warn "  Cannot detect country for: $base — leaving in place"
            (( warned++ )) || true
            continue
        fi
        cc_dir="$root/$cc"
        dest="$cc_dir/$base"
        run mkdir -p "$cc_dir"
        if [[ -f "$dest" ]]; then
            local s d
            s=$(stat -c%s "$f"); d=$(stat -c%s "$dest")
            if (( s == d )); then
                run rm -f "$f"
                log "  ✅ Removed top-level dup: $base"
                (( deleted++ )) || true
            else
                warn "  Size differs: $base (top=$s sub=$d) — manual check needed"
                (( warned++ )) || true
            fi
        else
            run mv -n "$f" "$dest"
            log "  🚚 Moved into $cc/: $base"
            (( moved++ )) || true
        fi
    done < <(find "$root" -maxdepth 1 -type f -print0)
    log "  📊 Tidy done — moved=$moved deleted=$deleted warnings=$warned"
}

# ── Pre-flight checks ─────────────────────────────────────────
preflight() {
    section "Pre-flight Checks"
    need_cmd rsync
    need_cmd find
    need_cmd stat
    need_cmd mv
    need_cmd rm
    [[ -d "$DATA_FOLDER" ]] || die "DATA_FOLDER not found: $DATA_FOLDER"
    ok "DATA_FOLDER exists: $DATA_FOLDER"
    if [[ -d "$DISTRO_FOLDER" ]]; then
        [[ -w "$DISTRO_FOLDER" ]] || die "DISTRO_FOLDER not writable: $DISTRO_FOLDER"
        ok "DISTRO_FOLDER writable: $DISTRO_FOLDER"
    else
        ok "DISTRO_FOLDER will be created: $DISTRO_FOLDER"
    fi
    local dest_mount free_gb
    dest_mount=$(df "$DATA_FOLDER" --output=avail | tail -1)
    free_gb=$(( dest_mount / 1024 / 1024 ))
    if (( free_gb < 100 )); then
        warn "Low disk space: ${free_gb}GB free — ingestion may fail"
    else
        ok "Disk space: ${free_gb}GB free"
    fi
    ok "Pre-flight complete"
}

# ── Usage ─────────────────────────────────────────────────────
usage() {
    cat <<'EOF'
Usage: bash ingest.sh [OPTIONS]
Options:
  --env FILE          Path to .folders.env (default: ./.folders.env)
  --dry-run           Preview actions without changing files
  --move              Move mode: delete source after verified copy
  --only LIST         Only run these dataset keys (comma-separated)
  --skip LIST         Skip these dataset keys (comma-separated)
  --log FILE          Write log to file (in addition to stdout)
  --skip-tidy         Skip MN tidy step
  --skip-cleanup      Skip duplicate cleanup step
  --skip-nextcloud    Skip Nextcloud file scan trigger
  --skip-qc           Skip QC verification step
Examples:
  sudo bash ingest.sh --dry-run
  sudo bash ingest.sh --only MNR_MEA,MNR_EUR --log /mnt/data/logs/ingest_q2.log
  sudo bash ingest.sh --move --skip SP_MEA
  sudo bash ingest.sh --only MN_MEA --skip-nextcloud
EOF
}

# ── Parse args ────────────────────────────────────────────────
while (( "$#" )); do
    case "$1" in
        --env)             ENV_FILE="$2"; shift 2 ;;
        --dry-run)         DRY_RUN=1; shift ;;
        --move)            MODE="move"; shift ;;
        --only)            ONLY="$2"; shift 2 ;;
        --skip)            SKIP="$2"; shift 2 ;;
        --log)             LOG_FILE="$2"; shift 2 ;;
        --skip-tidy)       SKIP_TIDY=1; shift ;;
        --skip-cleanup)    SKIP_CLEANUP=1; shift ;;
        --skip-nextcloud)  SKIP_NEXTCLOUD=1; shift ;;
        --skip-qc)         SKIP_QC=1; shift ;;
        -h|--help)         usage; exit 0 ;;
        *)                 die "Unknown option: $1" ;;
    esac
done

# ── Logging setup ─────────────────────────────────────────────
[[ -n "$LOG_FILE" ]] && { mkdir -p "$(dirname "$LOG_FILE")"; exec > >(tee -a "$LOG_FILE") 2>&1; }

# ── Load env ──────────────────────────────────────────────────
[[ -f "$ENV_FILE" ]] || die "Env file not found: $ENV_FILE"
set -a; source "$ENV_FILE"; set +a
: "${DISTRO_FOLDER:?DISTRO_FOLDER not set in $ENV_FILE}"
: "${DATA_FOLDER:?DATA_FOLDER not set in $ENV_FILE}"
DATA_FOLDER="${DATA_FOLDER%/}"

# ── Banner ────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════════╗"
echo -e "║   DATA INGESTION PIPELINE                    ║"
echo -e "║   $(date '+%Y-%m-%d %H:%M:%S')                     ║"
echo -e "╚══════════════════════════════════════════════╝${NC_COL}"
echo ""
(( DRY_RUN )) && warn "DRY-RUN MODE — no files will be changed"
log "Env:          $ENV_FILE"
log "Destination:  $DISTRO_FOLDER"
log "Source:       $DATA_FOLDER"
log "Mode:         $MODE"
[[ -n "$ONLY" ]] && log "Only:         $ONLY"
[[ -n "$SKIP" ]] && log "Skip:         $SKIP"
echo ""

# ── Pre-flight ────────────────────────────────────────────────
preflight

# ── Create base structure ─────────────────────────────────────
section "Creating Folder Structure"
run mkdir -p \
    "$DISTRO_FOLDER" \
    "$DISTRO_FOLDER/MN" \
    "$DISTRO_FOLDER/MN/MN_DOCUMENTATION" \
    "$DISTRO_FOLDER/MNR" \
    "$DISTRO_FOLDER/MNR/MNR_DOCUMENTATION" \
    "$DISTRO_FOLDER/PRODUCTS" \
    "$DISTRO_FOLDER/PRODUCTS/MN_PREMIUM_POI" \
    "$DISTRO_FOLDER/PRODUCTS/MN_PREMIUM_POI/MN_POI_DOCUMENTATION" \
    "$DISTRO_FOLDER/PRODUCTS/SPEED_PROFILES" \
    "$DISTRO_FOLDER/PRODUCTS/SPEED_PROFILES/SPEED_PROFILES_DOCUMENTATION" \
    "$DISTRO_FOLDER/PRODUCTS/MN_APT" \
    "$DISTRO_FOLDER/PRODUCTS/MN_APT/MN_APT_DOCUMENTATION"
ok "Folder structure ready"

# ── Main ingestion loop ───────────────────────────────────────
section "Ingesting Datasets"
START_TIME=$(date +%s)
RAN_ANY=0

while IFS= read -r key; do
    case "$key" in
        *_ZONES|*_DEST_SUBFOLDER) continue ;;
        MN_*|MNR_*|SP_*|APT_*|POI_*) ;;
        *) continue ;;
    esac
    should_run "$key" || { log "⏭️  Skipping (filter): $key"; continue; }
    local_val="${!key:-}"
    [[ -z "$local_val" ]] && { log "⏭️  Skipping (empty): $key"; continue; }
    src="$(resolve_source "$local_val")"
    type="${key%%_*}"
    region="${key#*_}"

    case "$type" in
        MNR)
            _zv="${key}_ZONES"; _zf="${!_zv:-}"
            process_mnr "$key" \
                "$src" \
                "$DISTRO_FOLDER/MNR/MNR_${region}" \
                "$DISTRO_FOLDER/MNR/MNR_DOCUMENTATION/MNR_DOCUMENTATION_${region}" \
                "$_zf" \
                "$DISTRO_FOLDER/MNR/MNR_MEA"
            ;;
        MN)
            _zv="${key}_ZONES"; _zf="${!_zv:-}"
            _dsv="${key}_DEST_SUBFOLDER"; _dsf="${!_dsv:-}"
            dest_mn="$DISTRO_FOLDER/MN/MN_${region}"
            [[ -n "$_dsf" ]] && dest_mn="$dest_mn/$_dsf"
            docs_mn="$DISTRO_FOLDER/MN/MN_DOCUMENTATION/MN_DOCUMENTATION_${region}"
            _fallback_mn="$DISTRO_FOLDER/MN/MN_EUR"
            process_flat "$key" "MultiNet" "$src" "$dest_mn" "$docs_mn" "$_zf" "$_fallback_mn"
            ;;
        SP)
            _zv="${key}_ZONES"; _zf="${!_zv:-}"
            dest_sp="$DISTRO_FOLDER/PRODUCTS/SPEED_PROFILES/SPEED_PROFILES_${region}"
            docs_sp="$DISTRO_FOLDER/PRODUCTS/SPEED_PROFILES/SPEED_PROFILES_DOCUMENTATION/SP_DOCUMENTATION_${region}"
            _fallback_sp="$DISTRO_FOLDER/PRODUCTS/SPEED_PROFILES/SPEED_PROFILES_MEA"
            process_flat "$key" "Speed Profiles" "$src" "$dest_sp" "$docs_sp" "$_zf" "$_fallback_sp"
            ;;
        POI)
            _zv="${key}_ZONES"; _zf="${!_zv:-}"
            dest_poi="$DISTRO_FOLDER/PRODUCTS/MN_PREMIUM_POI/MN_PREMIUM_POI_${region}"
            docs_poi="$DISTRO_FOLDER/PRODUCTS/MN_PREMIUM_POI/MN_POI_DOCUMENTATION/MN_POI_DOCUMENTATION_${region}"
            _fallback_poi="$DISTRO_FOLDER/PRODUCTS/MN_PREMIUM_POI/MN_PREMIUM_POI_MEA"
            process_flat "$key" "Premium POI" "$src" "$dest_poi" "$docs_poi" "$_zf" "$_fallback_poi"
            ;;
        APT)
            _zv="${key}_ZONES"; _zf="${!_zv:-}"
            dest_apt="$DISTRO_FOLDER/PRODUCTS/MN_APT/MN_APT_${region}"
            docs_apt="$DISTRO_FOLDER/PRODUCTS/MN_APT/MN_APT_DOCUMENTATION/MN_APT_DOCUMENTATION_${region}"
            process_flat "$key" "MN APT" "$src" "$dest_apt" "$docs_apt" "$_zf" ""
            ;;
        *)
            warn "Unknown dataset type for key: $key"
            ;;
    esac
    RAN_ANY=1
done < <(compgen -v | sort)

(( ! RAN_ANY )) && warn "No datasets ran — check .folders.env has MN_*, MNR_*, SP_*, POI_*, APT_* variables"

# ── Tidy MN files ─────────────────────────────────────────────
if (( ! SKIP_TIDY )); then
    section "Tidying MN Destination Folders"
    for mn_dir in "$DISTRO_FOLDER"/MN/MN_*/; do
        [[ -d "$mn_dir" ]] || continue
        [[ "$(basename "$mn_dir")" == "MN_DOCUMENTATION" ]] && continue
        tidy_mn "$mn_dir"
    done
    ok "MN tidy complete"
fi

# ── QC: Verify MNR module structure ───────────────────────────
if (( ! SKIP_QC )); then
    section "QC: Verifying MNR Module Structure"
    QC_ERRORS=0
    for mnr_dir in "$DISTRO_FOLDER"/MNR/MNR_*/; do
        [[ -d "$mnr_dir" ]] || continue
        mnr_key="$(basename "$mnr_dir")"
        [[ "$mnr_key" == "MNR_DOCUMENTATION" ]] && continue
        # Only QC datasets that were actually part of this run
        should_run "$mnr_key" || continue
        qc_mnr "$mnr_dir" || (( QC_ERRORS++ )) || true
    done
    if (( QC_ERRORS > 0 )); then
        warn "QC found issues in $QC_ERRORS region(s) — check output above"
    else
        ok "QC passed for all MNR regions"
    fi
fi

# ── Cleanup duplicates from source ────────────────────────────
if (( ! SKIP_CLEANUP )) && [[ "$MODE" == "copy" ]]; then
    section "Cleanup Source Duplicates"
    log "Checking for source files already in destination..."
    CLEANUP_DELETED=0
    while IFS= read -r key; do
        case "$key" in
            *_ZONES|*_DEST_SUBFOLDER) continue ;;
            MN_*|MNR_*|SP_*|APT_*|POI_*) ;;
            *) continue ;;
        esac
        should_run "$key" || continue
        local_val="${!key:-}"; [[ -z "$local_val" ]] && continue
        src_base="$(resolve_source "$local_val")"
        [[ -d "$src_base" ]] || continue
        type="${key%%_*}"; region="${key#*_}"
        case "$type" in
            MNR) dest_base="$DISTRO_FOLDER/MNR/MNR_${region}" ;;
            MN)  dest_base="$DISTRO_FOLDER/MN/MN_${region}" ;;
            SP)  dest_base="$DISTRO_FOLDER/PRODUCTS/SPEED_PROFILES/SPEED_PROFILES_${region}" ;;
            POI) dest_base="$DISTRO_FOLDER/PRODUCTS/MN_PREMIUM_POI/MN_PREMIUM_POI_${region}" ;;
            APT) dest_base="$DISTRO_FOLDER/PRODUCTS/MN_APT/MN_APT_${region}" ;;
            *)   continue ;;
        esac
        while IFS= read -r -d '' src_file; do
            fname="$(basename "$src_file")"
            dest_file="$dest_base/$fname"
            [[ -f "$dest_file" ]] || continue
            ss=$(stat -c%s "$src_file"); ds=$(stat -c%s "$dest_file")
            if (( ss == ds )); then
                run rm -f "$src_file"
                log "  🧹 Removed source dup: $fname"
                (( CLEANUP_DELETED++ )) || true
            fi
        done < <(find "$src_base" -type f ! -path "*/documentation/*" -print0 2>/dev/null)
    done < <(compgen -v | sort)
    ok "Cleanup done — removed $CLEANUP_DELETED duplicate source files"
fi

# ── Nextcloud file scan ───────────────────────────────────────
if (( ! SKIP_NEXTCLOUD )); then
    section "Triggering Nextcloud File Scan"
    if [[ -f "$NC_PATH/occ" ]]; then
        sudo -u www-data php "$NC_PATH/occ" files:scan --all \
            && ok "Nextcloud scan complete" \
            || warn "Nextcloud scan failed — run manually: sudo -u www-data php $NC_PATH/occ files:scan --all"
    else
        warn "Nextcloud occ not found at $NC_PATH — skipping scan"
    fi
fi

# ── Summary ───────────────────────────────────────────────────
END_TIME=$(date +%s)
ELAPSED=$(( END_TIME - START_TIME ))
MINS=$(( ELAPSED / 60 ))
SECS=$(( ELAPSED % 60 ))
echo ""
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════╗"
echo -e "║   INGESTION COMPLETE                         ║"
echo -e "╠══════════════════════════════════════════════╣"
printf  "║   %-42s ║\n" "Copied:    $COPIED file(s)"
printf  "║   %-42s ║\n" "Skipped:   $SKIPPED file(s) (already up to date)"
printf  "║   %-42s ║\n" "Errors:    $ERRORS"
printf  "║   %-42s ║\n" "Duration:  ${MINS}m ${SECS}s"
printf  "║   %-42s ║\n" "Mode:      $MODE"
(( DRY_RUN )) && printf "║   %-42s ║\n" "⚠️  DRY-RUN — no files changed"
echo -e "╚══════════════════════════════════════════════╝${NC_COL}"
echo ""
(( ERRORS > 0 )) && warn "$ERRORS error(s) occurred — check log for details"
exit 0
