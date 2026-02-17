# gmail-r2-backup

Python CLI that backs up a consumer Gmail account to Cloudflare R2 (S3-compatible) using the Gmail API.

## What it does

- Uses the Gmail API (OAuth) to fetch new messages incrementally.
- Stores each message as a gzipped RFC822 `.eml.gz` plus a small metadata JSON.
- Uploads to R2 via S3 API.
- Keeps local state (history id + sqlite index) so repeat runs are fast.

## Requirements

- Python 3.11+
- A Google Cloud OAuth "Desktop app" client JSON with Gmail API enabled.
- Cloudflare R2 bucket + credentials (Access Key ID / Secret).

## Install (editable)

```bash
cd gmail-r2-backup
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e .
```

## Configure R2

Set env vars (recommended):

```bash
export R2_ACCOUNT_ID="..."
export AWS_ACCESS_KEY_ID="..."
export AWS_SECRET_ACCESS_KEY="..."
export R2_BUCKET="my-bucket"
export R2_PREFIX="gmail-backup"
```

The endpoint is derived from `R2_ACCOUNT_ID`:
`https://<ACCOUNT_ID>.r2.cloudflarestorage.com`

## Authenticate Gmail

1. Create an OAuth client (Desktop app) and download `credentials.json`.
2. Run:

```bash
gmail-r2-backup auth --credentials /path/to/credentials.json
```

This opens a browser for OAuth consent and stores a refresh token locally.

## Run a backup

First run (optionally limit scope):

```bash
# backups everything it can discover; if you have a huge mailbox consider --since
gmail-r2-backup backup --since "2024-01-01"
```

Subsequent runs use Gmail History for incrementals:

```bash
gmail-r2-backup backup
```

## Periodic mode

Run forever, backing up every 15 minutes:

```bash
gmail-r2-backup daemon --every 900
```

In practice, cron/systemd is usually better than a long-running Python loop.

## Restore (same-account disaster recovery)

There is a restore command that can re-insert backed up raw emails into Gmail.

Important notes:
- This requires Gmail write scopes; you must run `auth` with `--write` first.
- Restore cannot preserve Gmail message IDs/thread IDs 1:1 (Gmail assigns new ones), but message content and attachments are restored from the raw RFC822.
- Dedupe is best-effort: it skips messages already present when `Message-ID` is available (via Gmail search), and it records a local restore index to make re-runs safe.

Dry-run (no Gmail writes):

```bash
gmail-r2-backup restore
```

Apply restore (writes to Gmail):

```bash
gmail-r2-backup restore --apply
```

## Storage layout in R2

- `<prefix>/messages/<messageId>.eml.gz`
- `<prefix>/messages/<messageId>.json`
- `<prefix>/state/state.json`

## Notes / limitations

- Incrementals track "message added" events. Label-only changes and deletions are not currently represented.
- If Gmail returns `404` on history (startHistoryId too old), the tool falls back to a query-based scan.
