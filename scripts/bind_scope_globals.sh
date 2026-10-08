#!/bin/bash
# ══════════════════════════════════════════════════════════════════
# bind_scope_globals.sh
#
# Scope Telematics receives two "Global" datasets via Nextcloud:
#   - MN Global   : all MN regions + docs, EXCLUDING MN_3D_TRACKING
#   - POI Global  : the 6 main Premium POI regions + docs
#
# Nextcloud external storage won't follow symlinks, so these are served
# by bind-mounting the real region folders into curated custom dirs that
# Scope's mounts point at:
#   /mnt/data/custom/mn_global   -> Nextcloud mount /Mn_Global
#   /mnt/data/custom/poi_global  -> Nextcloud mount /Poi_Global
#
# Bind mounts do NOT survive reboot, and after a quarterly rollover
# DISTRIBUTION_CURRENT repoints to the new quarter — so these binds must
# be recreated at boot AND after every rollover. This script is
# idempotent: safe to run any number of times.
#
# Run:
#   - at boot (systemd unit scope-globals.service)
#   - as the LAST step of the quarterly rollover, after the symlink flips
#
# After running, scans are triggered automatically at the end.
# ══════════════════════════════════════════════════════════════════
set -uo pipefail

MN_SRC="/mnt/data/DISTRIBUTION_CURRENT/MN"
POI_SRC="/mnt/data/DISTRIBUTION_CURRENT/PRODUCTS/MN_PREMIUM_POI"
MN_DEST="/mnt/data/custom/mn_global"
POI_DEST="/mnt/data/custom/poi_global"
NC="sudo -u www-data php /var/www/html/nextcloud/occ"

# MN: all regions + docs, 3D_TRACKING deliberately excluded
MN_ITEMS=(MN_MEA MN_EUR MN_NAM MN_LAM MN_SEA MN_OCE MN_IND MN_ISR MN_CAS MN_S_O MN_DOCUMENTATION)
# POI: 6 main regions + docs (SA/ZAF subsets are inside MEA, excluded to avoid dupes)
POI_ITEMS=(MN_PREMIUM_POI_MEA MN_PREMIUM_POI_EUR MN_PREMIUM_POI_NAM MN_PREMIUM_POI_SEA MN_PREMIUM_POI_OCE MN_PREMIUM_POI_CAS MN_POI_DOCUMENTATION)

bind_set () {
    local src_base="$1" dest="$2"; shift 2
    local items=("$@")
    echo "[$(date '+%F %T')] Binding into $dest"
    mkdir -p "$dest"

    # Remove stray bound dirs not in the allowed list
    for existing in "$dest"/*/; do
        [ -e "$existing" ] || continue
        name="$(basename "$existing")"
        keep=0
        for i in "${items[@]}"; do [ "$name" = "$i" ] && keep=1 && break; done
        if [ "$keep" -eq 0 ]; then
            mountpoint -q "$existing" && umount "$existing"
            rmdir "$existing" 2>/dev/null || true
            echo "  removed stray: $name"
        fi
    done

    # (Re)bind each allowed item to the CURRENT quarter's real path
    for i in "${items[@]}"; do
        local s="$src_base/$i" d="$dest/$i"
        if [ ! -d "$s" ]; then echo "  ⚠️  source missing: $i"; continue; fi
        mkdir -p "$d"
        mountpoint -q "$d" && umount "$d" 2>/dev/null || true
        mount --bind "$s" "$d"
    done
    echo "  $(mount | grep -c "$dest/") binds active"
}

bind_set "$MN_SRC"  "$MN_DEST"  "${MN_ITEMS[@]}"
bind_set "$POI_SRC" "$POI_DEST" "${POI_ITEMS[@]}"

echo "[$(date '+%F %T')] Triggering Nextcloud scans"
$NC files:scan --path="scope_telematics/files/Mn_Global"  >/dev/null 2>&1 || echo "  ⚠️  MN scan failed"
$NC files:scan --path="scope_telematics/files/Poi_Global" >/dev/null 2>&1 || echo "  ⚠️  POI scan failed"
echo "[$(date '+%F %T')] Done"
