# GeoINT Distribution Dashboard

A Flask-based web dashboard for managing quarterly TomTom geospatial data distribution to clients via Nextcloud.

## Features
- Quarterly pipeline: folder structure → download → ingest → provision → notify
- SFTP push to external partners (Tracker, Riskscape) with background nohup process
- Client management and Nextcloud account provisioning
- Client file uploads
- Email notifications with Jira comment generation
- Health check, backup, disk cleanup tools
- Next quarter wizard (auto-calculates version numbers)

## Setup
- Copy `config/.env.template` to `config/.env` and fill in values
- Install dependencies: `pip install -r requirements.txt`
- Run: `python3 app.py`
- Or as a service: `systemctl start ddq-ui`

## Configuration
All credentials (SMTP, SFTP passwords, Nextcloud) are loaded from `.env` — never hardcoded.
