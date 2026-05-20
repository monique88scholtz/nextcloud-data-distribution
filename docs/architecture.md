# Architecture & Design Notes

## Problem

A data distribution team shares quarterly geospatial datasets with 10 clients.
Previously managed manually via OwnCloud (now decommissioned). Each client must
**only** see their own data — never another client's files.

## Solution Architecture

```
┌─────────────────────────────────────────────────────────┐
│                   Ubuntu Server                          │
│                                                          │
│  ┌──────────────┐    ┌──────────────────────────────┐   │
│  │  Apache 2    │    │        Nextcloud              │   │
│  │  (HTTPS)     │───▶│  ┌─────────────────────────┐ │   │
│  └──────────────┘    │  │  User: 3D_Tracking       │ │   │
│                      │  │  Groups: mn_3D_tracking   │ │   │
│  ┌──────────────┐    │  │          premium_poi_zaf  │ │   │
│  │  MariaDB     │    │  └─────────────────────────┘ │   │
│  │  (metadata)  │───▶│                               │   │
│  └──────────────┘    │  ┌─────────────────────────┐ │   │
│                      │  │  External Storage Mounts  │ │   │
│  ┌──────────────┐    │  │  /mnt/data/MN/...   ─────┼─┼──▶ Group A only
│  │  Redis       │    │  │  /mnt/data/MNR/...  ─────┼─┼──▶ Group B only
│  │  (cache)     │───▶│  └─────────────────────────┘ │   │
│  └──────────────┘    └──────────────────────────────┘   │
│                                                          │
│  ┌──────────────────────────────────────────────────┐   │
│  │  /mnt/data/DISTRIBUTION_Q1_2026/                 │   │
│  │  ├── MN/          ├── MNR/       ├── MAPIT/       │   │
│  │  └── PRODUCTS/                                   │   │
│  └──────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
```

## Access Isolation Model

```
CSV Row  →  DirectoryPath  →  Nextcloud Group  →  Client User
                                    │
                          Only users IN that group
                          can see that mount point
```

## Quarterly Update Flow

```
New CSV arrives
     │
     ▼
validate_csv.py  ──── fails? ──▶ Fix CSV
     │ passes
     ▼
update_quarter.py --dry-run   ──▶ Review diff
     │ looks good
     ▼
update_quarter.py             ──▶ Applied to Nextcloud
     │
     ▼
files:scan --all              ──▶ New files visible to clients
```

## Key Design Decisions

**Groups as the unit of access** — instead of sharing individual folders with
individual users, directories are mounted and scoped to a group. Users are
assigned to groups. This makes quarterly updates a matter of adjusting group
membership rather than re-sharing dozens of folders.

**UUID passwords from CSV** — the existing workflow already assigns UUID
passwords per client. These are loaded directly from the CSV so no manual
password management is needed.

**External storage over file copy** — the raw data stays on the existing
`/mnt/data/` paths. Nextcloud mounts these as external storage, so there is
no duplication and files remain manageable outside Nextcloud if needed.

**Python provisioning over bash** — the provisioning logic uses Python +
pandas because the CSV has a non-trivial structure (forward-filled CLIENT and
PASSWORD columns, blank separator rows). Python handles this more robustly
than bash string parsing.
