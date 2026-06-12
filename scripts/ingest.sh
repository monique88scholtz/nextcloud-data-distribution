#!/usr/bin/env bash
# =============================================================
#  ingest.sh — MASTER DATA INGESTION PIPELINE
#  ─────────────────────────────────────────────────────────────
#  Single entry point for the full quarterly ingestion process:
#    1. Pre-flight checks (disk space, source paths, permissions)
#    2. Build distribution folder structure
#    3. Copy/move files with atomic writes and size verification
#    4. Tidy MN files into country subfolders
#    5. Clean up duplicates from source
#    6. Trigger Nextcloud file scan
#    7. Print summary report
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

# ── Defaults ─────────────────────────────────────────────────
ENV_FILE="./.folders.env"
DRY_RUN=0
MODE="copy"       # copy | move
ONLY=""           # e.g. "MNR_MEA,MNR_EUR"
SKIP=""
LOG_FILE=""
SKIP_TIDY=0
SKIP_CLEANUP=0
SKIP_NEXTCLOUD=0
NC_PATH="/var/www/html/nextcloud"

# ── Colours ───────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

log()     { echo -e "[$(date '+%F %T')] $*"; }
ok()      { echo -e "[$(date '+%F %T')] ${GREEN}✅${NC} $*"; }
warn()    { echo -e "[$(date '+%F %T')] ${YELLOW}⚠️ ${NC} $*"; }
die()     { echo -e "[$(date '+%F %T')] ${RED}❌${NC} $*"; exit 1; }
section() { echo -e "\n${BOLD}${CYAN}━━━  $*  ━━━${NC}\n"; }

run() {
    if (( DRY_RUN )); then log "DRYRUN: $*"; return 0; fi
    log "RUN: $*"; "$@"
}

on_err() { local c=$?; log "${RED}ERROR (exit=$c) at line $1: $2${NC}"; exit "$c"; }
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
    # Match -mn- or -li- prefix files: mea2026_06_000-gdfas-mn-ago-ago.7z.001
    #                                   mea2026_06_000-osl-li-ago-ago.7z.001
    # Country code is always the 4th dash-separated segment
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

    # Atomic copy via temp file
    local tmp
    tmp="$(dirname "$dest")/.tmp.$(basename "$dest").$$"
    run rsync -a --no-perms --no-owner --no-group "$src" "$tmp"
    run mv -f "$tmp" "$dest"

    # Verify size unless dry-run
    if (( ! DRY_RUN )); then
        local new_size
        new_size=$(stat -c%s "$dest")
        if (( new_size != src_size )); then
            warn "  Size mismatch after copy! src=$src_size dest=$new_size — keeping dest, NOT removing src"
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

# ── Process one dataset ───────────────────────────────────────
process_dataset() {
    local key="$1" label="$2" src_base="$3" dest_base="$4" docs_dest="$5"
    local zones_filter="${6:-}"       # Optional comma-separated country codes
    local fallback_base="${7:-}"      # Optional already-ingested parent folder

    # Subsets must preserve the parent source. Even when the dashboard runs
    # ingest.sh with --move to save disk space, zone-filtered datasets are copied
    # only. The parent/full regional dataset is the one allowed to move/delete
    # the source files.
    if [[ -n "$zones_filter" ]]; then
        PRESERVE_SOURCE=1
    else
        PRESERVE_SOURCE=0
    fi

    log "────────────────────────────────────"
    log "📦 $label ($key)"
    log "   src : $src_base"
    [[ -n "$fallback_base" ]] && log "   fallback src: $fallback_base"
    log "   dest: $dest_base"
    log "   docs: $docs_dest"
    log "   mode: $MODE | preserve-source: ${PRESERVE_SOURCE:-0} | dry-run: $DRY_RUN"
    log "────────────────────────────────────"

    # If the original download source has already been moved into the parent
    # distribution folder, continue from that parent folder. This keeps the UI
    # workflow recoverable without exposing a separate repair button.
    if ! has_any_file "$src_base"; then
        if [[ -n "$zones_filter" && -n "$fallback_base" ]] && has_any_file "$fallback_base"; then
            warn "Source download is missing or empty; using already-organised parent folder as source"
            src_base="$fallback_base"
        fi
    fi

    if [[ ! -d "$src_base" ]] || ! has_any_file "$src_base"; then
        if [[ -n "$fallback_base" ]] && has_any_file "$fallback_base"; then
            warn "Source download missing; using fallback: $fallback_base"
            src_base="$fallback_base"
        elif has_any_file "$dest_base"; then
            ok "Already ingested: $key — data in $dest_base"
            return 0
        else
            warn "Source missing, skipping: $src_base"
            return 0
        fi
    fi

    run mkdir -p "$dest_base" "$docs_dest"

    # Copy documentation and tools folders if present
    # Check src_base first (e.g. data/mea/documentation)
    # then check parent folder (download root has documentation/ and tools/)
    for _doc_src in "$src_base/documentation" "$(dirname "$src_base")/documentation" "$(dirname "$(dirname "$src_base")")/documentation"; do
        if [[ -d "$_doc_src" ]]; then
            log "  📄 Copying documentation from: $_doc_src"
            run rsync -a --no-perms --no-owner --no-group "$_doc_src/" "$docs_dest/"
            break
        fi
    done
    for _tools_src in "$src_base/tools" "$(dirname "$src_base")/tools" "$(dirname "$(dirname "$src_base")")/tools"; do
        if [[ -d "$_tools_src" ]]; then
            log "  🔧 Copying tools from: $_tools_src"
            run rsync -a --no-perms --no-owner --no-group "$_tools_src/" "$docs_dest/tools/"
            break
        fi
    done

    # Discover country folders — optionally filtered by zones
    mapfile -t all_countries < <(
        find "$src_base" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' \
          | grep -v '^documentation$' | grep -v '^[[:space:]]*$' | sort -u
    )

    # Remove empty entries from mapfile result
    mapfile -t all_countries < <(printf "%s\n" "${all_countries[@]}" | grep -v "^$")
    all_countries=("${all_countries[@]:-}")
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
        # Flat dataset — no country subfolders
        log "  ➡️  Flat dataset mode"
        while IFS= read -r -d '' file; do
            local fname cc
            fname="$(basename "$file")"
            if [[ "$fname" == *documentation* ]]; then
                continue
            fi
            if [[ "$key" == MN_* || "$key" == POI_* || "$key" == SP_* ]]; then
                cc="$(mn_country_from_filename "$fname")"
                if [[ -n "$cc" ]]; then
                    copy_file "$file" "$dest_base/$cc/$fname"
                else
                    copy_file "$file" "$dest_base/$fname"
                fi
            else
                copy_file "$file" "$dest_base/$fname"
            fi
        done < <(find "$src_base" -type f -print0 2>/dev/null)
    else
        # Country folder mode. Preserve the internal structure under each
        # country folder instead of flattening everything into dest/$cc/.
        log "  🌍 Found ${#countries[@]} country folder(s)"
        for cc in "${countries[@]}"; do
            local country_src="$src_base/$cc"
            if [[ -d "$country_src" ]]; then
                while IFS= read -r -d '' file; do
                    local rel fname
                    fname="$(basename "$file")"
                    rel="${file#"$country_src/"}"
                    [[ "$fname" == *documentation* ]] && continue
                    # Route model.tar.gz to documentation folder
                    if [[ "$fname" == "model.tar.gz" ]]; then
                        copy_file "$file" "$docs_dest/$fname"
                    else
                        copy_file "$file" "$dest_base/$cc/$rel"
                    fi
                done < <(find "$country_src" -type f -print0 2>/dev/null)
            else
                warn "Country folder not found: $country_src"
            fi
        done
    fi

    ok "Finished: $label ($key)"
}

# ── Tidy MN root-level files into country subfolders ─────────
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

    # Check DATA_FOLDER exists
    [[ -d "$DATA_FOLDER" ]] || die "DATA_FOLDER not found: $DATA_FOLDER"
    ok "DATA_FOLDER exists: $DATA_FOLDER"

    # Check DISTRO_FOLDER is writable (or can be created)
    if [[ -d "$DISTRO_FOLDER" ]]; then
        [[ -w "$DISTRO_FOLDER" ]] || die "DISTRO_FOLDER not writable: $DISTRO_FOLDER"
        ok "DISTRO_FOLDER writable: $DISTRO_FOLDER"
    else
        ok "DISTRO_FOLDER will be created: $DISTRO_FOLDER"
    fi

    # Disk space check — warn if < 100GB free on destination
    local dest_mount free_gb
    dest_mount=$(df "$DATA_FOLDER" --output=avail | tail -1)
    free_gb=$(( dest_mount / 1024 / 1024 ))
    if (( free_gb < 100 )); then
        warn "Low disk space on destination: ${free_gb}GB free — ingestion may fail"
    else
        ok "Disk space: ${free_gb}GB free"
    fi

    # Check which source paths exist
    local missing=0
    while IFS= read -r key; do
        case "$key" in *_ZONES|*_DEST_SUBFOLDER) continue ;; MN_*|MNR_*|SP_*|APT_*|POI_*) ;; *) continue ;; esac
        local val="${!key:-}"
        [[ -z "$val" ]] && { warn "  $key is empty — will skip"; continue; }
        local src
        src="$(resolve_source "$val")"
        if [[ -d "$src" ]]; then
            ok "  Source OK: $key → $src"
        else
            # Check if already ingested into distribution folder
            type_pf="${key%%_*}"; region_pf="${key#*_}"
            case "$type_pf" in
                MNR) distro_check="$DISTRO_FOLDER/MNR/MNR_${region_pf}" ;;
                MN)  distro_check="$DISTRO_FOLDER/MN/MN_${region_pf}" ;;
                SP)  distro_check="$DISTRO_FOLDER/PRODUCTS/SPEED_PROFILES/SPEED_PROFILES_${region_pf}" ;;
                POI) distro_check="$DISTRO_FOLDER/PRODUCTS/MN_PREMIUM_POI/MN_PREMIUM_POI_${region_pf}" ;;
                APT) distro_check="$DISTRO_FOLDER/PRODUCTS/MN_APT/MN_APT_${region_pf}" ;;
                *)   distro_check="" ;;
            esac
            if [[ -n "$distro_check" ]] && has_any_file "$distro_check"; then
                ok "  Already ingested: $key → $distro_check"
            else
                warn "  Source MISSING: $key → $src"
                (( missing++ )) || true
            fi
        fi
    done < <(compgen -v | sort)

    (( missing > 0 )) && warn "$missing source path(s) missing — those datasets will be skipped"
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

Examples:
  sudo bash ingest.sh --dry-run
  sudo bash ingest.sh --only MNR_MEA,MNR_EUR --log /mnt/data/logs/ingest_q1.log
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
        -h|--help)         usage; exit 0 ;;
        *)                 die "Unknown option: $1" ;;
    esac
done

# ── Logging setup ─────────────────────────────────────────────
[[ -n "$LOG_FILE" ]] && { mkdir -p "$(dirname "$LOG_FILE")"; exec > >(tee -a "$LOG_FILE") 2>&1; }

# ── Load env ──────────────────────────────────────────────────
[[ -f "$ENV_FILE" ]] || die "Env file not found: $ENV_FILE"
# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a

: "${DISTRO_FOLDER:?DISTRO_FOLDER not set in $ENV_FILE}"
: "${DATA_FOLDER:?DATA_FOLDER not set in $ENV_FILE}"
DATA_FOLDER="${DATA_FOLDER%/}"

# ── Banner ────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════════╗"
echo -e "║   DATA INGESTION PIPELINE                    ║"
echo -e "║   $(date '+%Y-%m-%d %H:%M:%S')                     ║"
echo -e "╚══════════════════════════════════════════════╝${NC}"
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
    case "$key" in *_ZONES|*_DEST_SUBFOLDER) continue ;; MN_*|MNR_*|SP_*|APT_*|POI_*) ;; *) continue ;; esac
    should_run "$key" || { log "⏭️  Skipping (filter): $key"; continue; }

    local_val="${!key:-}"
    [[ -z "$local_val" ]] && { log "⏭️  Skipping (empty): $key"; continue; }

    src="$(resolve_source "$local_val")"
    type="${key%%_*}"
    region="${key#*_}"

    case "$type" in
        MNR)
            # Zone filter: read MNR_SOUTHERN_AFRICA_ZONES etc from env
            _zv="${key}_ZONES"; _zf="${!_zv:-}"
            process_dataset "$key" "MultiNet-R" \
                "$src" \
                "$DISTRO_FOLDER/MNR/MNR_${region}" \
                "$DISTRO_FOLDER/MNR/MNR_DOCUMENTATION/MNR_DOCUMENTATION_${region}" \
                "$_zf" \
                "$DISTRO_FOLDER/MNR/MNR_MEA"
            ;;
        MN)
            # MN files are flat .7z.001 files — sort into country folders by filename
            _zv="${key}_ZONES"; _zf="${!_zv:-}"
            _dsv="${key}_DEST_SUBFOLDER"; _dsf="${!_dsv:-}"
            dest_mn="$DISTRO_FOLDER/MN/MN_${region}"
            [[ -n "$_dsf" ]] && dest_mn="$dest_mn/$_dsf"
            docs_mn="$DISTRO_FOLDER/MN/MN_DOCUMENTATION/MN_DOCUMENTATION_${region}"
            # Fallback: if source is empty, use already-ingested parent EUR folder
            _src_mn="$src"
            PRESERVE_SOURCE=0
            if [[ -n "$_zf" ]]; then
                PRESERVE_SOURCE=1
                if ! has_any_file "$_src_mn"; then
                    _fallback_mn="$DISTRO_FOLDER/MN/MN_EUR"
                    if has_any_file "$_fallback_mn"; then
                        log "  ℹ️  Using fallback source: $_fallback_mn"
                        _src_mn="$_fallback_mn"
                    fi
                fi
            fi
            log "────────────────────────────────────"
            log "📦 MultiNet ($key)"
            log "   src : $_src_mn"
            log "   dest: $dest_mn"
            log "────────────────────────────────────"
            if has_any_file "$_src_mn"; then
                run mkdir -p "$dest_mn" "$docs_mn"
                # Copy documentation
                for _doc_src in "$(dirname "$src")/documentation"; do
                    if [[ -d "$_doc_src" ]]; then
                        log "  📄 Copying documentation from: $_doc_src"
                        run rsync -a --no-perms --no-owner --no-group "$_doc_src/" "$docs_mn/"
                        break
                    fi
                done
                # Sort flat files into country subfolders with optional zone filter
                log "  ➡️  Flat MN mode — sorting into country folders"
                while IFS= read -r -d $'\0' file; do
                    fname="$(basename "$file")"
                    cc="$(mn_country_from_filename "$fname")"
                    # Apply zone filter if set
                    if [[ -n "$_zf" && -n "$cc" ]]; then
                        IFS=',' read -r -a _zones <<< "$_zf"
                        _match=0
                        for _z in "${_zones[@]}"; do
                            [[ "${cc,,}" == "${_z,,}" ]] && { _match=1; break; }
                        done
                        (( _match )) || continue
                    fi
                    if [[ -n "$cc" ]]; then
                        copy_file "$file" "$dest_mn/$cc/$fname"
                    else
                        copy_file "$file" "$dest_mn/$fname"
                    fi
                done < <(find "$_src_mn" -maxdepth 2 -type f -name "*.7z.001" -print0 2>/dev/null)
                ok "Finished: MultiNet ($key)"
            elif has_any_file "$dest_mn"; then
                ok "Already ingested: MultiNet ($key) — data in $dest_mn"
            else
                warn "MN source missing or empty — skipping: $src"
            fi
            ;;
        SP)
            # SP files are flat .7z.001 files — sort into country folders by filename
            _zv="${key}_ZONES"; _zf="${!_zv:-}"
            dest_sp="$DISTRO_FOLDER/PRODUCTS/SPEED_PROFILES/SPEED_PROFILES_${region}"
            docs_sp="$DISTRO_FOLDER/PRODUCTS/SPEED_PROFILES/SPEED_PROFILES_DOCUMENTATION/SP_DOCUMENTATION_${region}"
            _src_sp="$src"
            PRESERVE_SOURCE=0
            if [[ -n "$_zf" ]]; then
                PRESERVE_SOURCE=1
                if ! has_any_file "$_src_sp"; then
                    _fallback_sp="$DISTRO_FOLDER/PRODUCTS/SPEED_PROFILES/SPEED_PROFILES_MEA"
                    if has_any_file "$_fallback_sp"; then
                        log "  ℹ️  Using fallback source: $_fallback_sp"
                        _src_sp="$_fallback_sp"
                    fi
                fi
            fi
            log "────────────────────────────────────"
            log "📦 Speed Profiles ($key)"
            log "   src : $_src_sp"
            log "   dest: $dest_sp"
            log "────────────────────────────────────"
            if has_any_file "$_src_sp"; then
                run mkdir -p "$dest_sp" "$docs_sp"
                for _doc_src in "$(dirname "$src")/documentation"; do
                    if [[ -d "$_doc_src" ]]; then
                        log "  📄 Copying documentation from: $_doc_src"
                        run rsync -a --no-perms --no-owner --no-group "$_doc_src/" "$docs_sp/"
                        break
                    fi
                done
                log "  ➡️  Flat SP mode — sorting into country folders"
                while IFS= read -r -d $'\0' file; do
                    fname="$(basename "$file")"
                    cc="$(mn_country_from_filename "$fname")"
                    if [[ -n "$_zf" && -n "$cc" ]]; then
                        IFS=',' read -r -a _zones <<< "$_zf"
                        _match=0
                        for _z in "${_zones[@]}"; do
                            [[ "${cc,,}" == "${_z,,}" ]] && { _match=1; break; }
                        done
                        (( _match )) || continue
                    fi
                    if [[ -n "$cc" ]]; then
                        copy_file "$file" "$dest_sp/$cc/$fname"
                    else
                        copy_file "$file" "$dest_sp/$fname"
                    fi
                done < <(find "$_src_sp" -maxdepth 2 -type f -name "*.7z.001" -print0 2>/dev/null)
                ok "Finished: Speed Profiles ($key)"
            elif has_any_file "$dest_sp"; then
                ok "Already ingested: Speed Profiles ($key) — data in $dest_sp"
            else
                warn "SP source missing or empty — skipping: $src"
            fi
            ;;
        POI)
            # POI files are flat .7z.001 files — sort into country folders by filename
            _zv="${key}_ZONES"; _zf="${!_zv:-}"
            dest_poi="$DISTRO_FOLDER/PRODUCTS/MN_PREMIUM_POI/MN_PREMIUM_POI_${region}"
            docs_poi="$DISTRO_FOLDER/PRODUCTS/MN_PREMIUM_POI/MN_POI_DOCUMENTATION/MN_POI_DOCUMENTATION_${region}"
            fallback_poi="$DISTRO_FOLDER/PRODUCTS/MN_PREMIUM_POI/MN_PREMIUM_POI_MEA"
            log "────────────────────────────────────"
            log "📦 Premium POI ($key)"
            log "   src : $src"
            log "   dest: $dest_poi"
            log "────────────────────────────────────"
            _src_poi="$src"
            PRESERVE_SOURCE=0
            # Zone-filtered subsets must NEVER delete source files
            if [[ -n "$_zf" ]]; then
                PRESERVE_SOURCE=1
            fi
            # For zone-filtered subsets, fall back to already-ingested MEA folder
            if [[ -n "$_zf" ]] && (! has_any_file "$_src_poi") && has_any_file "$fallback_poi"; then
                log "  ℹ️  Using fallback source: $fallback_poi"
                _src_poi="$fallback_poi"
                PRESERVE_SOURCE=1
            fi
            if has_any_file "$_src_poi"; then
                run mkdir -p "$dest_poi" "$docs_poi"
                for _doc_src in "$(dirname "$src")/documentation"; do
                    if [[ -d "$_doc_src" ]]; then
                        log "  📄 Copying documentation from: $_doc_src"
                        run rsync -a --no-perms --no-owner --no-group "$_doc_src/" "$docs_poi/"
                        break
                    fi
                done
                log "  ➡️  Flat POI mode — sorting into country folders"
                while IFS= read -r -d $'\0' file; do
                    fname="$(basename "$file")"
                    cc="$(mn_country_from_filename "$fname")"
                    # Apply zone filter if set
                    if [[ -n "$_zf" ]]; then
                        IFS=',' read -r -a _zones <<< "$_zf"
                        _match=0
                        for _z in "${_zones[@]}"; do
                            [[ "${cc,,}" == "${_z,,}" ]] && { _match=1; break; }
                        done
                        (( _match )) || continue
                    fi
                    if [[ -n "$cc" ]]; then
                        copy_file "$file" "$dest_poi/$cc/$fname"
                    else
                        copy_file "$file" "$dest_poi/$fname"
                    fi
                done < <(find "$_src_poi" -maxdepth 2 -type f -name "*.7z.001" -print0 2>/dev/null)
                ok "Finished: Premium POI ($key)"
            elif has_any_file "$dest_poi"; then
                ok "Already ingested: Premium POI ($key) — data in $dest_poi"
            else
                warn "POI source missing or empty — skipping: $src"
            fi
            ;;
        APT)
            process_dataset "$key" "MN APT" \
                "$src" \
                "$DISTRO_FOLDER/PRODUCTS/MN_APT/MN_APT_${region}" \
                "$DISTRO_FOLDER/PRODUCTS/MN_APT/MN_APT_DOCUMENTATION/MN_APT_DOCUMENTATION_${region}"
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
    # Dynamic paths from env — no hardcoding
    for mn_dir in "$DISTRO_FOLDER"/MN/MN_*/; do
        [[ -d "$mn_dir" ]] || continue
        [[ "$(basename "$mn_dir")" == "MN_DOCUMENTATION" ]] && continue
        tidy_mn "$mn_dir"
    done
    ok "MN tidy complete"
fi

# ── Cleanup duplicates from source ───────────────────────────
if (( ! SKIP_CLEANUP )) && [[ "$MODE" == "copy" ]]; then
    section "Cleanup Source Duplicates"
    log "Checking for source files already in destination (same size)..."

    CLEANUP_DELETED=0
    while IFS= read -r key; do
        case "$key" in *_ZONES|*_DEST_SUBFOLDER) continue ;; MN_*|MNR_*|SP_*|APT_*|POI_*) ;; *) continue ;; esac
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
        sudo -u www-data php "$NC_PATH/occ" files:scan --all && ok "Nextcloud scan complete" || warn "Nextcloud scan failed — run manually: sudo -u www-data php $NC_PATH/occ files:scan --all"
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
printf  "║   %-42s ║\n" "Replaced:  $REPLACED file(s)"
printf  "║   %-42s ║\n" "Errors:    $ERRORS"
printf  "║   %-42s ║\n" "Duration:  ${MINS}m ${SECS}s"
printf  "║   %-42s ║\n" "Mode:      $MODE"
(( DRY_RUN )) && printf "║   %-42s ║\n" "⚠️  DRY-RUN — no files changed"
echo -e "╚══════════════════════════════════════════════╝${NC}"
echo ""

(( ERRORS > 0 )) && warn "$ERRORS error(s) occurred — check log for details"
exit 0
root@content:/opt/nextcloud-setup/nextcloud-data-distribution# 
