#!/usr/bin/env python3
"""
agent_notify.py
───────────────
AUTOMATED CLIENT NOTIFICATION AGENT
Sends each client a personalised email with:
  - Their login credentials
  - List of datasets they have access to
  - Access link

Usage:
  python3 agents/agent_notify.py --env config/.env --quarter Q2_2026
  python3 agents/agent_notify.py --env config/.env --client esri --quarter Q2_2026
  python3 agents/agent_notify.py --env config/.env --quarter Q2_2026 --dry-run

Requirements:
  pip install pandas python-dotenv

SMTP config needed in .env:
  SMTP_HOST=smtp.gmail.com
  SMTP_PORT=587
  SMTP_USER=your@email.com
  SMTP_PASSWORD=your_app_password
  SMTP_FROM=noreply@geoint.africa
  PORTAL_URL=http://YOUR_SERVER_IP
"""

import argparse
import os
import smtplib
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

GREEN  = "\033[0;32m"; YELLOW = "\033[1;33m"
RED    = "\033[0;31m"; CYAN   = "\033[0;36m"
BOLD   = "\033[1m";    NC     = "\033[0m"

def info(m):  print(f"{CYAN}[INFO]{NC}  {m}")
def ok(m):    print(f"{GREEN}[OK]{NC}    {m}")
def warn(m):  print(f"{YELLOW}[WARN]{NC}  {m}")
def error(m): print(f"{RED}[ERROR]{NC} {m}"); sys.exit(1)


def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["CLIENT"]   = df["CLIENT"].ffill()
    df["PASSWORD"] = df["PASSWORD"].ffill()
    df = df.dropna(subset=["CLIENT", "GROUP", "DirectoryPath"])
    for col in df.columns:
        df[col] = df[col].astype(str).str.strip()
    return df


def build_client_map(df: pd.DataFrame) -> dict:
    clients = {}
    for _, row in df.iterrows():
        name = row["CLIENT"]
        if name not in clients:
            clients[name] = {
                "password": row["PASSWORD"],
                "username": name.lower().replace(" ", "_"),
                "datasets": [],
            }
        clients[name]["datasets"].append(row.get("DATA", row["GROUP"]))
    return clients


def build_email_html(display_name: str, username: str, password: str,
                     datasets: list, quarter: str, portal_url: str) -> str:
    dataset_rows = "".join([
        f'<tr><td style="padding:8px 12px;border-bottom:1px solid #f0f0f0;">📦 {d}</td></tr>'
        for d in datasets
    ])
    return f"""
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f5f5f5;font-family:Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f5f5;padding:40px 0;">
    <tr><td align="center">
      <table width="580" cellpadding="0" cellspacing="0"
             style="background:#ffffff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">

        <!-- Header -->
        <tr>
          <td style="background:#1a1a2e;padding:32px 40px;">
            <h1 style="margin:0;color:#ffffff;font-size:22px;font-weight:600;">
              Data Distribution Portal
            </h1>
            <p style="margin:6px 0 0;color:#888;font-size:13px;">{quarter} Release</p>
          </td>
        </tr>

        <!-- Body -->
        <tr>
          <td style="padding:36px 40px;">
            <p style="margin:0 0 16px;color:#333;font-size:15px;">
              Dear <strong>{display_name}</strong>,
            </p>
            <p style="margin:0 0 24px;color:#555;font-size:14px;line-height:1.6;">
              Your <strong>{quarter}</strong> data is now available on the distribution portal.
              Please use the credentials below to access your datasets.
            </p>

            <!-- Credentials box -->
            <table width="100%" cellpadding="0" cellspacing="0"
                   style="background:#f8f9ff;border:1px solid #e0e4ff;border-radius:6px;margin-bottom:28px;">
              <tr>
                <td style="padding:20px 24px;">
                  <p style="margin:0 0 4px;color:#888;font-size:11px;text-transform:uppercase;letter-spacing:1px;">
                    Access Link
                  </p>
                  <p style="margin:0 0 16px;color:#1a1a2e;font-size:14px;font-weight:600;">
                    <a href="{portal_url}" style="color:#4a6cf7;">{portal_url}</a>
                  </p>
                  <p style="margin:0 0 4px;color:#888;font-size:11px;text-transform:uppercase;letter-spacing:1px;">
                    Username
                  </p>
                  <p style="margin:0 0 16px;color:#1a1a2e;font-size:14px;font-family:monospace;
                            background:#fff;padding:8px 12px;border-radius:4px;border:1px solid #e0e0e0;">
                    {username}
                  </p>
                  <p style="margin:0 0 4px;color:#888;font-size:11px;text-transform:uppercase;letter-spacing:1px;">
                    Password
                  </p>
                  <p style="margin:0;color:#1a1a2e;font-size:14px;font-family:monospace;
                            background:#fff;padding:8px 12px;border-radius:4px;border:1px solid #e0e0e0;">
                    {password}
                  </p>
                </td>
              </tr>
            </table>

            <!-- Datasets -->
            <p style="margin:0 0 12px;color:#333;font-size:14px;font-weight:600;">
              Your Datasets ({len(datasets)})
            </p>
            <table width="100%" cellpadding="0" cellspacing="0"
                   style="border:1px solid #e0e0e0;border-radius:6px;overflow:hidden;margin-bottom:28px;">
              {dataset_rows}
            </table>

            <p style="margin:0 0 8px;color:#555;font-size:13px;line-height:1.6;">
              If you experience any issues accessing your data, please contact us.
            </p>
          </td>
        </tr>

        <!-- Footer -->
        <tr>
          <td style="background:#f8f8f8;padding:20px 40px;border-top:1px solid #eee;">
            <p style="margin:0;color:#aaa;font-size:12px;">
              This is an automated notification. Please do not reply to this email.
            </p>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>
</body>
</html>
"""


def send_email(to_email: str, subject: str, html_body: str,
               smtp_config: dict, dry_run: bool = False) -> bool:
    if dry_run:
        print(f"\n  {YELLOW}[DRY-RUN]{NC} Would send to: {to_email}")
        print(f"  Subject: {subject}")
        return True

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = smtp_config["from"]
        msg["To"]      = to_email
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP(smtp_config["host"], smtp_config["port"]) as server:
            server.starttls()
            server.login(smtp_config["user"], smtp_config["password"])
            server.sendmail(smtp_config["from"], to_email, msg.as_string())
        return True
    except Exception as e:
        warn(f"  Email failed for {to_email}: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Client Notification Agent")
    parser.add_argument("--env",      default="config/.env")
    parser.add_argument("--csv",      default="data/client_distribution.csv")
    parser.add_argument("--quarter",  required=True, help="e.g. Q2_2026")
    parser.add_argument("--client",   help="Send to one client only")
    parser.add_argument("--dry-run",  action="store_true")
    args = parser.parse_args()

    load_dotenv(args.env)

    # SMTP config from .env
    smtp_config = {
        "host":     os.getenv("SMTP_HOST", "smtp.gmail.com"),
        "port":     int(os.getenv("SMTP_PORT", "587")),
        "user":     os.getenv("SMTP_USER", ""),
        "password": os.getenv("SMTP_PASSWORD", ""),
        "from":     os.getenv("SMTP_FROM", os.getenv("SSL_EMAIL", "")),
    }
    portal_url = os.getenv("PORTAL_URL", f"http://{os.getenv('NC_SERVER_IP', 'YOUR_SERVER_IP')}/")

    if not smtp_config["user"] and not args.dry_run:
        error("SMTP_USER not set in .env — add SMTP settings or use --dry-run")

    df = load_csv(args.csv)
    client_map = build_client_map(df)

    if args.client:
        client_map = {k: v for k, v in client_map.items()
                      if k.lower() == args.client.lower()}
        if not client_map:
            error(f"Client '{args.client}' not found in CSV")

    info(f"Sending {args.quarter} notifications to {len(client_map)} client(s)")
    if args.dry_run:
        warn("DRY-RUN mode — no emails will be sent")

    sent = 0
    failed = 0

    for display_name, client in client_map.items():
        # Build email — use username@domain as fallback if no email on file
        to_email = f"{client['username']}@example.com"  # Replace with real email lookup
        subject  = f"[{args.quarter}] Your Data Distribution Access — {display_name}"
        html     = build_email_html(
            display_name = display_name,
            username     = client["username"],
            password     = client["password"],
            datasets     = client["datasets"],
            quarter      = args.quarter,
            portal_url   = portal_url,
        )

        info(f"  Notifying: {display_name} ({to_email})")
        if send_email(to_email, subject, html, smtp_config, args.dry_run):
            ok(f"  Sent: {display_name}")
            sent += 1
        else:
            failed += 1

    print(f"\n{'─'*40}")
    ok(f"Notifications complete — {sent} sent, {failed} failed")


if __name__ == "__main__":
    main()
