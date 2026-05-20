#!/usr/bin/env python3
"""
validate_csv.py
───────────────
Validates the distribution CSV before provisioning.
Run locally or in CI to catch problems early.

Usage:
  python3 scripts/validate_csv.py --csv data/client_distribution.csv
"""
import argparse
import sys
import pandas as pd

RED   = "\033[0;31m"; GREEN = "\033[0;32m"
YELLOW= "\033[1;33m"; NC    = "\033[0m"

errors   = []
warnings = []

def err(msg):  errors.append(msg);   print(f"{RED}[FAIL]{NC} {msg}")
def warn(msg): warnings.append(msg); print(f"{YELLOW}[WARN]{NC} {msg}")
def ok(msg):   print(f"{GREEN}[OK]{NC}  {msg}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", required=True)
    args = parser.parse_args()

    df = pd.read_csv(args.csv)
    df["CLIENT"]   = df["CLIENT"].ffill()
    df["PASSWORD"] = df["PASSWORD"].ffill()
    df_clean = df.dropna(subset=["CLIENT", "GROUP", "DirectoryPath"])

    print(f"\nValidating: {args.csv}")
    print(f"Total rows (incl. blanks): {len(df)}")
    print(f"Valid data rows:           {len(df_clean)}\n")

    # Required columns
    required = {"CLIENT", "PASSWORD", "DATA", "GROUP", "DirectoryPath"}
    missing = required - set(df.columns)
    if missing:
        err(f"Missing columns: {missing}")
    else:
        ok("All required columns present")

    # Every client has a password
    clients_no_pass = []
    for client, group in df_clean.groupby("CLIENT"):
        if group["PASSWORD"].isna().all() or (group["PASSWORD"] == "nan").all():
            clients_no_pass.append(client)
    if clients_no_pass:
        err(f"Clients missing password: {clients_no_pass}")
    else:
        ok("All clients have passwords")

    # No duplicate (CLIENT, DirectoryPath) combinations
    dupes = df_clean[df_clean.duplicated(subset=["CLIENT", "DirectoryPath"], keep=False)]
    if not dupes.empty:
        warn(f"Duplicate CLIENT+DirectoryPath rows found:\n{dupes[['CLIENT','DirectoryPath']].to_string()}")
    else:
        ok("No duplicate client+path combinations")

    # Paths start with /
    bad_paths = df_clean[~df_clean["DirectoryPath"].str.startswith("/")]
    if not bad_paths.empty:
        warn(f"Paths not starting with /:\n{bad_paths['DirectoryPath'].tolist()}")
    else:
        ok("All paths start with /")

    # Usernames (lowercase client names) would be unique
    usernames = df_clean["CLIENT"].str.lower().str.replace(" ", "_").unique()
    if len(usernames) != len(df_clean["CLIENT"].unique()):
        err("Username collision — two clients would get the same lowercase username")
    else:
        ok(f"{len(usernames)} unique usernames — no collisions")

    # Summary
    print(f"\n{'─'*50}")
    if errors:
        print(f"{RED}❌ {len(errors)} error(s) found — fix before provisioning{NC}")
        sys.exit(1)
    elif warnings:
        print(f"{YELLOW}⚠️  {len(warnings)} warning(s) — review before provisioning{NC}")
    else:
        print(f"{GREEN}✅  CSV is valid — ready to provision{NC}")

if __name__ == "__main__":
    main()
