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

# Recommended: uv (fast Python package manager)
uv sync
source .venv/bin/activate

# Alternatively: pip
# python3 -m venv .venv
# source .venv/bin/activate
# pip install -U pip
# pip install -e .
```

## Development checks

```bash
uv run pytest
uv run mypy gmail_r2_backup
```

## Configure R2

Set env vars (recommended):

```bash
export R2_ACCOUNT_ID="..."
export R2_ENDPOINT_URL=""   # optional override, e.g. https://<account>.<jurisdiction>.r2.cloudflarestorage.com
export AWS_ACCESS_KEY_ID="..."
export AWS_SECRET_ACCESS_KEY="..."
export R2_BUCKET="my-bucket"
export R2_PREFIX="gmail-backup"
```

Tip: the CLI auto-loads a local `.env` file if present (and `.env` is in `.gitignore`).

The endpoint is derived from `R2_ACCOUNT_ID` by default:
`https://<ACCOUNT_ID>.r2.cloudflarestorage.com`

If you're using a jurisdiction-specific endpoint (for example `...eu.r2.cloudflarestorage.com`), set `R2_ENDPOINT_URL` explicitly.

## Authenticate Gmail

This project uses the OAuth 2.0 "installed app" flow (a desktop application).
When you run `gmail-r2-backup auth ...`, it starts a temporary local HTTP server on `localhost` (random free port),
opens your browser for consent, receives the OAuth authorization code on the loopback redirect URL
(`http://localhost:<port>/`), then exchanges it for tokens and stores a refresh token locally.

### Create Google OAuth Credentials (Desktop App)

In Google Cloud Console:

1. Create/select a project.
2. Enable the **Gmail API**.
3. Configure **APIs & Services -> OAuth consent screen**:
   - Use **External** for consumer Gmail accounts.
   - If the app is in "Testing", add your Gmail address under **Test users**.
   - Add scopes you plan to use:
     - Backup-only: `https://www.googleapis.com/auth/gmail.readonly`
     - Restore: `https://www.googleapis.com/auth/gmail.insert` and `https://www.googleapis.com/auth/gmail.modify`
4. Create credentials:
   - **APIs & Services -> Credentials -> Create Credentials -> OAuth client ID**
   - Application type: **Desktop app**
   - Download the JSON file (often named something like `client_secret_....json`)

Note: for **Desktop app** credentials you do not manually configure redirect URIs; the installed-app flow uses the loopback
redirect on `http://localhost` (the library picks a free port automatically).

### Run Auth

Run (backup/read-only):

```bash
gmail-r2-backup auth --credentials /path/to/credentials.json
```

Alternative (no JSON file): set env vars and run without `--credentials`:

```bash
export GOOGLE_CLIENT_ID="..."
export GOOGLE_CLIENT_SECRET="..."
gmail-r2-backup auth
```

Note: passing `--client-secret ...` directly on the command line is supported, but not recommended because it may end up in shell history.

Run (restore/write scopes):

```bash
gmail-r2-backup auth --credentials /path/to/credentials.json --write
```

This opens a browser for OAuth consent and stores a refresh token locally (see "Local state" below).

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

If you back up multiple Gmail accounts into the same bucket, you can derive `R2_PREFIX` automatically from the authenticated Gmail address:

```bash
gmail-r2-backup backup --auto-prefix
```

Progress and concurrency (defaults are safe, tune as needed):

```bash
gmail-r2-backup backup --workers 4 --progress-every 200
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

If the backup used `--auto-prefix`, restore must also use it:

```bash
gmail-r2-backup restore --apply --auto-prefix
```

Restore can also run concurrently:

```bash
gmail-r2-backup restore --apply --workers 4 --progress-every 200
```

Idempotency notes:
- Backup is idempotent: it keeps a local sqlite index of uploaded message IDs and also persists state to R2.
- Restore is best-effort idempotent: it skips messages already present when `Message-ID` exists and records a local restore index.
  In addition, successful restore operations write per-message restore markers back to R2 under `state/restore/` so re-running from another machine won't duplicate work.

## Storage layout in R2

- `<prefix>/messages/<messageId>.eml.gz`
- `<prefix>/messages/<messageId>.json`
- `<prefix>/state/state.json`

## Notes / limitations

- Incrementals track "message added" events. Label-only changes and deletions are not currently represented.
- If Gmail returns `404` on history (startHistoryId too old), the tool falls back to a query-based scan.

## Deploy on Coolify (scheduled backups, multi-account)

This works well as a "worker" container with Coolify Scheduled Tasks.

1. Deploy this repo with the included `Dockerfile` (or `docker-compose.yml` in Compose mode).
2. Mount a persistent volume at `/state` so tokens and the sqlite index survive redeploys.
3. Set runtime env vars in Coolify:
   - `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`
   - `R2_BUCKET`, `R2_ENDPOINT_URL` (and optional `R2_REGION`)
   - Do not set `R2_PREFIX` if you want to use `--auto-prefix` for multiple accounts.
4. Create one Scheduled Task per Gmail account, each using a different `--state-dir` under `/state`:

```bash
gmail-r2-backup backup --state-dir /state/felix.vemmer@gmail.com --workers 12 --gzip-level 1 --progress-every 200 --auto-prefix
```

### OAuth bootstrapping in Coolify

The OAuth flow opens a browser, so you typically run `auth` locally and then copy the generated `token.json` into the server's `/state/<account>/` directory.

Note: the provided `docker-compose.yml` overrides the Dockerfile entrypoint to run `sleep infinity` so the container stays up for Scheduled Tasks. If you change the Compose file, keep that pattern.

The Compose file also defines a simple `healthcheck` (runs `gmail-r2-backup --help`) so Coolify can mark the worker container as healthy.
