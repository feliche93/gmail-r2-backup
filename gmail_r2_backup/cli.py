from __future__ import annotations

import datetime as dt
import sys
import time
from pathlib import Path
from typing import Optional

import typer

from .config import R2Config, load_app_config
from .gmail import GmailClient
from .backup import BackupRunner
from .r2 import R2Client
from .restore import RestoreRunner
from .state import StateStore


app = typer.Typer(
    name="gmail-r2-backup",
    help="Back up a consumer Gmail account to Cloudflare R2 (S3-compatible) using the Gmail API.",
    add_completion=False,
)


def _parse_since(s: Optional[str]) -> Optional[dt.date]:
    if not s:
        return None
    try:
        return dt.date.fromisoformat(s)
    except ValueError as e:
        raise typer.BadParameter("Expected YYYY-MM-DD") from e


@app.command()
def auth(
    credentials: Path = typer.Option(..., "--credentials", help="Path to Google OAuth client JSON."),
    write: bool = typer.Option(
        False,
        "--write",
        help="Request Gmail write scopes (needed for restore). Default is readonly for backup.",
    ),
) -> None:
    cfg = load_app_config()
    r2 = R2Config.from_env_or_config(cfg)
    st = StateStore.open_default()
    gmail = GmailClient.from_oauth_desktop_flow(
        credentials_path=str(credentials),
        token_store=st,
        scopes=[GmailClient.SCOPE_INSERT, GmailClient.SCOPE_MODIFY] if write else [GmailClient.SCOPE_READONLY],
    )
    # Touch the profile to validate token and capture current history id for later runs.
    profile = gmail.get_profile()
    st.write_state({"historyId": profile.get("historyId"), "authedAt": int(time.time())})
    print("OAuth OK. Current historyId:", profile.get("historyId"))
    # r2 is loaded just to validate env/config early; no calls.
    _ = r2


@app.command()
def backup(
    since: Optional[str] = typer.Option(
        None,
        "--since",
        help="Limit initial/fallback scan to messages after YYYY-MM-DD (Gmail search query).",
    ),
    max_messages: int = typer.Option(
        0,
        "--max-messages",
        min=0,
        help="Optional cap for testing (0 = unlimited).",
    ),
) -> None:
    cfg = load_app_config()
    r2 = R2Config.from_env_or_config(cfg)
    st = StateStore.open_default()
    gmail = GmailClient.from_stored_token(
        token_store=st,
        scopes=[GmailClient.SCOPE_READONLY],
    )
    runner = BackupRunner(gmail=gmail, r2=r2, state=st)
    since_date = _parse_since(since)
    stats = runner.run_backup(since=since_date, max_messages=max_messages)
    print(
        "Backup complete:",
        f"uploaded={stats.uploaded}",
        f"skipped={stats.skipped}",
        f"errors={stats.errors}",
    )
    if stats.errors != 0:
        raise typer.Exit(code=2)

@app.command()
def restore(
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Actually restore messages into Gmail. Without this flag, runs a dry-run (no Gmail writes).",
    ),
    since: Optional[str] = typer.Option(
        None,
        "--since",
        help="Only consider backed up messages with internalDate on/after YYYY-MM-DD (UTC, best-effort).",
    ),
    max_messages: int = typer.Option(
        0,
        "--max-messages",
        min=0,
        help="Optional cap for testing (0 = unlimited).",
    ),
) -> None:
    cfg = load_app_config()
    r2cfg = R2Config.from_env_or_config(cfg)
    st = StateStore.open_default()
    gmail = GmailClient.from_stored_token(
        token_store=st,
        scopes=[GmailClient.SCOPE_INSERT, GmailClient.SCOPE_MODIFY],
    )
    r2 = R2Client(r2cfg)
    runner = RestoreRunner(gmail=gmail, r2=r2, state=st)

    since_date = _parse_since(since)
    stats = runner.run_restore(apply=apply, since=since_date, max_messages=max_messages)
    mode = "RESTORE" if apply else "DRY-RUN"
    print(
        f"{mode} complete:",
        f"considered={stats.considered}",
        f"restored={stats.restored}",
        f"skipped={stats.skipped}",
        f"errors={stats.errors}",
    )
    if apply and stats.errors != 0:
        raise typer.Exit(code=2)


@app.command()
def daemon(
    every: int = typer.Option(..., "--every", min=30, help="Interval in seconds (>= 30)."),
    since: Optional[str] = typer.Option(None, "--since", help="Same as backup --since (used for fallback scans)."),
    max_messages: int = typer.Option(0, "--max-messages", min=0),
) -> None:
    while True:
        try:
            backup(since=since, max_messages=max_messages)
        except typer.Exit as e:
            # backup() uses Exit(code=2) to signal "completed with errors".
            if getattr(e, "exit_code", 0) not in (0, None):
                print("Backup run exited non-zero:", e.exit_code, file=sys.stderr)
            else:
                raise
        except KeyboardInterrupt:
            raise typer.Exit(code=130)
        except Exception as e:
            print("Backup run crashed:", repr(e), file=sys.stderr)
        time.sleep(int(every))


def main(argv: Optional[list[str]] = None) -> None:
    # Keep a main() entrypoint for the console_script in pyproject.toml.
    # Typer/Click handle exit codes via exceptions.
    app(prog_name="gmail-r2-backup", args=argv)


if __name__ == "__main__":
    main()
