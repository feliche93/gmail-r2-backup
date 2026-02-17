# Agent Instructions (gmail-r2-backup)

This repository is a small Python 3.11+ CLI that backs up Gmail messages to Cloudflare R2 (S3-compatible) using the Gmail API and boto3.

## CLI Framework

The CLI uses [Typer](https://typer.tiangolo.com/) (Click-based). Prefer keeping subcommands and options stable (`auth`, `backup`, `daemon`) and rely on Typer types/options for validation.

## Quick Start (Local Dev)

```bash
cd <repo-root>
uv sync
source .venv/bin/activate

# CLI help
gmail-r2-backup --help
gmail-r2-backup backup --help
```

## Commands You Can Run

- OAuth (creates/updates local token state):
  - `gmail-r2-backup auth --credentials /path/to/credentials.json`
  - For restore scopes: `gmail-r2-backup auth --credentials /path/to/credentials.json --write`
- One backup pass (writes to R2):
  - `gmail-r2-backup backup`
  - `gmail-r2-backup backup --since YYYY-MM-DD` (limits fallback scan)
  - `gmail-r2-backup backup --max-messages N` (dev/test cap)
- Restore from R2 back into Gmail:
  - Dry-run: `gmail-r2-backup restore`
  - Apply: `gmail-r2-backup restore --apply`
- Simple loop mode:
  - `gmail-r2-backup daemon --every 900`

## Required Environment (R2)

The tool reads R2 config from environment variables (preferred) or from a user config file:

- `R2_ACCOUNT_ID` (required)
- `R2_BUCKET` (required)
- `R2_PREFIX` (optional, default `gmail-backup`)
- `R2_REGION` (optional, default `auto`)
- `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` (required by boto3 for auth)

Endpoint is derived as: `https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com`.

## Local State and Secret Files (Do Not Commit)

This CLI stores user-specific state using `platformdirs`:

- App config JSON:
  - `platformdirs.user_config_dir("gmail-r2-backup")/config.json`
- State directory (created automatically):
  - `platformdirs.user_data_dir("gmail-r2-backup")/state/`
  - Contains:
    - `token.json` (Gmail OAuth refresh token, sensitive)
    - `state.json` (history id, timestamps)
    - `index.sqlite3` (uploaded-message index)

Never commit any of these files, paste their contents into PRs, or log token contents.

## R2 Object Layout

The backup writes:

- `<prefix>/messages/<messageId>.eml.gz`
- `<prefix>/messages/<messageId>.json`
- `<prefix>/state/state.json`

## Safety Rules (Important)

1. Do not run `auth`, `backup`, or `daemon` unless the user explicitly asks, because they:
   - Open a browser for OAuth consent (auth), and/or
   - Upload data to a real R2 bucket (backup/daemon).
2. When debugging behavior, prefer `--max-messages N` and use a dedicated test bucket and/or unique `R2_PREFIX`.
3. Avoid printing message contents. Message bodies are stored in `.eml.gz` and can include sensitive data.
4. If you need to reproduce a bug, request sanitized logs/metadata only (message id, counts, exception type).

## Codebase Pointers

- CLI entrypoint: `gmail_r2_backup/cli.py`
- Gmail API wrapper: `gmail_r2_backup/gmail.py`
- R2 wrapper (boto3): `gmail_r2_backup/r2.py`
- Backup orchestration: `gmail_r2_backup/backup.py`
- Local state store (sqlite + json): `gmail_r2_backup/state.py`
- R2 config loading: `gmail_r2_backup/config.py`

## Lightweight Checks (No Test Suite Assumed)

If no test tooling is set up, use these as minimum sanity checks after code changes:

```bash
python -m compileall gmail_r2_backup
gmail-r2-backup --help
```
