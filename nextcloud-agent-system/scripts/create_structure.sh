#!/usr/bin/env bash
# =============================================================
#  create_structure.sh
#  Creates the full DISTRIBUTION folder structure for a quarter
#  WITHOUT needing any data files.
#
#  Run this as soon as a new quarter starts to prep the server.
#  Ingest.sh will then copy files into the existing structure.
#
#  Usage:
#    bash create_structure.sh --quarter Q2_2026
#    bash create_structure.sh --quarter Q2_2026 --dry-run
# =============================================================

set -Eeuo pipefail

BASE="/mnt/data"
QUARTER=""
DRY_RUN=0

GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; NC='\033[0m'
ok()      { echo -e "${GREEN}✅${NC}  $*"; }
info()    { echo -e "${CYAN}📁${NC}  $*"; }
warn()    { echo -e "${YELLOW}⚠️ ${NC}  $*"; }
section() { echo -e "\n${BOLD}${CYAN}━━━  $*  ━━━${NC}\n"; }

usage() {
    echo "Usage: bash create_structure.sh --quarter Q2_2026 [--dry-run]"
    exit 0
}

while (( "$#" )); do
    case "$1" in
        --quarter) QUARTER="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage ;;
        *) echo "Unknown: $1"; usage ;;
    esac
done

[[ -z "$QUARTER" ]] && echo "ERROR: --quarter required" && usage

DISTRO="$BASE/DISTRIBUTION_${QUARTER}"
(( DRY_RUN )) && warn "DRY-RUN — no folders will be created"

mkdir_p() {
    local path="$1"
    if (( DRY_RUN )); then
        info "Would create: $path"
    else
        mkdir -p "$path"
        info "$path"
    fi
}

echo ""
echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════════════╗"
echo -e "║  Creating structure for: ${QUARTER}               ║"
echo -e "║  Base: ${DISTRO}  ║"
echo -e "╚══════════════════════════════════════════════════╝${NC}"
echo ""

# ── MNR ───────────────────────────────────────────────────────
section "MultiNet-R (MNR)"
mkdir_p "$DISTRO/MNR"
for region in MEA EUR SOUTHERN_AFRICA ZAF ZAF_PLUS NAM LAM SEA CAS IND OCE ISR S_O GLOBAL; do
    mkdir_p "$DISTRO/MNR/MNR_${region}"
done
mkdir_p "$DISTRO/MNR/MNR_DOCUMENTATION"
for region in MEA EUR SOUTHERN_AFRICA ZAF NAM LAM SEA CAS IND OCE ISR S_O; do
    mkdir_p "$DISTRO/MNR/MNR_DOCUMENTATION/MNR_DOCUMENTATION_${region}"
done

# ── MN ────────────────────────────────────────────────────────
section "MultiNet (MN)"
mkdir_p "$DISTRO/MN"
for region in MEA EUR ZAF ZAF_PLUS SOUTHERN_AFRICA 3D_TRACKING; do
    mkdir_p "$DISTRO/MN/MN_${region}"
done
mkdir_p "$DISTRO/MN/MN_GLOBAL"
for region in MEA EUR NAM LAM SEA CAS IND OCE ISR S_O; do
    mkdir_p "$DISTRO/MN/MN_GLOBAL/${region}"
done
mkdir_p "$DISTRO/MN/MN_DOCUMENTATION"
for region in MEA EUR NAM LAM SEA CAS IND OCE ISR S_O; do
    mkdir_p "$DISTRO/MN/MN_DOCUMENTATION/MN_DOCUMENTATION_${region}"
done

# ── PRODUCTS ──────────────────────────────────────────────────
section "Products"
mkdir_p "$DISTRO/PRODUCTS"

# Speed Profiles
mkdir_p "$DISTRO/PRODUCTS/SPEED_PROFILES"
for region in MEA EUR ZAF ZAF_PLUS SOUTHERN_AFRICA NAM LAM SEA CAS IND OCE ISR GBR S_O; do
    mkdir_p "$DISTRO/PRODUCTS/SPEED_PROFILES/SPEED_PROFILES_${region}"
done
mkdir_p "$DISTRO/PRODUCTS/SPEED_PROFILES/SPEED_PROFILES_DOCUMENTATION"
for region in MEA EUR NAM LAM SEA CAS IND OCE ISR S_O; do
    mkdir_p "$DISTRO/PRODUCTS/SPEED_PROFILES/SPEED_PROFILES_DOCUMENTATION/SP_DOCUMENTATION_${region}"
done

# Premium POI
mkdir_p "$DISTRO/PRODUCTS/MN_PREMIUM_POI"
for region in MEA ZAF ZAF_PLUS SOUTHERN_AFRICA GLOBAL NAMIB; do
    mkdir_p "$DISTRO/PRODUCTS/MN_PREMIUM_POI/MN_PREMIUM_POI_${region}"
done
mkdir_p "$DISTRO/PRODUCTS/MN_PREMIUM_POI/MN_POI_DOCUMENTATION"
for region in MEA EUR NAM LAM SEA CAS IND OCE ISR S_O; do
    mkdir_p "$DISTRO/PRODUCTS/MN_PREMIUM_POI/MN_POI_DOCUMENTATION/MN_POI_DOCUMENTATION_${region}"
done

# APT
mkdir_p "$DISTRO/PRODUCTS/MN_APT"
for region in MEA EUR ZAF ZAF_PLUS SOUTHERN_AFRICA NAM LAM SEA CAS IND OCE ISR S_O; do
    mkdir_p "$DISTRO/PRODUCTS/MN_APT/MN_APT_${region}"
done
mkdir_p "$DISTRO/PRODUCTS/MN_APT/MN_APT_DOCUMENTATION"
for region in MEA EUR NAM LAM SEA CAS IND OCE ISR S_O; do
    mkdir_p "$DISTRO/PRODUCTS/MN_APT/MN_APT_DOCUMENTATION/MN_APT_DOCUMENTATION_${region}"
done

# MAPIT
section "MapIT"
mkdir_p "$DISTRO/MAPIT"
mkdir_p "$DISTRO/MAPIT/SQL/MAPIT_SQL_AFR"
mkdir_p "$DISTRO/MAPIT/SQL/MAPIT_SQL_ZAF"
mkdir_p "$DISTRO/MAPIT/SQL/MAPIT_SQL_ZAF_PLUS"
mkdir_p "$DISTRO/MAPIT/SHAPEFILE/MAPIT_SHP_AFR"
mkdir_p "$DISTRO/MAPIT/SHAPEFILE/MAPIT_SHP_ZAF"
mkdir_p "$DISTRO/MAPIT/SHAPEFILE/MAPIT_SHP_SOUTHERN_AFRICA"
mkdir_p "$DISTRO/MAPIT/MAPIT_DOCUMENTATION"
mkdir_p "$DISTRO/MAPIT/POSTGRESQL"
mkdir_p "$DISTRO/MAPIT/CSV"

# ── Set permissions ───────────────────────────────────────────
if (( ! DRY_RUN )); then
    chown -R www-data:www-data "$DISTRO" 2>/dev/null || \
        warn "Could not chown to www-data — run: sudo chown -R www-data:www-data $DISTRO"
fi

# ── Count folders ─────────────────────────────────────────────
if (( ! DRY_RUN )); then
    FOLDER_COUNT=$(find "$DISTRO" -type d | wc -l)
else
    FOLDER_COUNT="(dry-run)"
fi

echo ""
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════════╗"
echo -e "║  Structure created: ${QUARTER}                  ║"
printf  "║  %-48s ║\n" "Folders: $FOLDER_COUNT"
printf  "║  %-48s ║\n" "Path: $DISTRO"
echo -e "║                                                  ║"
echo -e "║  Next steps:                                     ║"
echo -e "║  1. Update .folders.env with new DISTRO_FOLDER   ║"
echo -e "║  2. Update download.sh with new version number   ║"
echo -e "║  3. When data arrives: bash download.sh          ║"
echo -e "║  4. Then: bash ingest.sh                         ║"
echo -e "╚══════════════════════════════════════════════════╝${NC}"
echo ""
