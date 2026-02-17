from __future__ import annotations

import datetime as dt
import os
import sys
import time
from pathlib import Path
from typing import Optional

import typer

from .config import AppConfig, R2Config, load_app_config
from .gmail import GmailClient
from .backup import BackupRunner
from .backup import BackupStats
from .r2 import R2Client
from .restore import RestoreRunner
from .restore import RestoreStats
from .state import StateStore
from .naming import r2_prefix_from_email


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


def _load_dotenv() -> None:
    # Convenience for local usage. Does not override existing environment by default.
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        return


def _prefix_is_explicit(cfg: AppConfig) -> bool:
    # Treat prefix as explicit if it is present (even if empty).
    if "R2_PREFIX" in os.environ:
        return True
    try:
        return cfg.r2 is not None and cfg.r2.prefix is not None
    except Exception:
        return False


def _maybe_auto_prefix(
    *,
    r2cfg: R2Config,
    state: StateStore,
    gmail: GmailClient,
    enabled: bool,
    explicit: bool,
) -> R2Config:
    if not enabled or explicit:
        return r2cfg
    email = state.read_state().get("emailAddress")
    if not isinstance(email, str) or not email:
        profile = gmail.get_profile()
        email = profile.get("emailAddress")
    if not isinstance(email, str) or not email:
        return r2cfg
    return r2cfg.model_copy(update={"prefix": r2_prefix_from_email(email)})


@app.command()
def auth(
    credentials: Optional[Path] = typer.Option(
        None,
        "--credentials",
        help="Path to Google OAuth client JSON (Desktop app).",
    ),
    client_id: Optional[str] = typer.Option(
        None,
        "--client-id",
        envvar="GOOGLE_CLIENT_ID",
        help="Google OAuth client id (Desktop app). Prefer env var GOOGLE_CLIENT_ID.",
    ),
    client_secret: Optional[str] = typer.Option(
        None,
        "--client-secret",
        envvar="GOOGLE_CLIENT_SECRET",
        help="Google OAuth client secret (Desktop app). Prefer env var GOOGLE_CLIENT_SECRET.",
    ),
    write: bool = typer.Option(
        False,
        "--write",
        help="Request Gmail write scopes (needed for restore). Default is readonly for backup.",
    ),
) -> None:
    _load_dotenv()
    if credentials and (client_id or client_secret):
        raise typer.BadParameter("Use either --credentials OR --client-id/--client-secret (not both).")
    if not credentials and (not client_id or not client_secret):
        raise typer.BadParameter(
            "Missing OAuth credentials. Provide --credentials <json> or both --client-id and --client-secret."
        )

    cfg = load_app_config()
    r2 = R2Config.from_env_or_config(cfg)
    st = StateStore.open_default()
    scopes = [GmailClient.SCOPE_INSERT, GmailClient.SCOPE_MODIFY] if write else [GmailClient.SCOPE_READONLY]
    if credentials:
        gmail = GmailClient.from_oauth_desktop_flow(
            credentials_path=str(credentials),
            token_store=st,
            scopes=scopes,
        )
    else:
        gmail = GmailClient.from_oauth_desktop_flow_client_secrets(
            client_id=str(client_id),
            client_secret=str(client_secret),
            token_store=st,
            scopes=scopes,
        )
    # Touch the profile to validate token and capture current history id for later runs.
    profile = gmail.get_profile()
    st.write_state(
        {
            "historyId": profile.get("historyId"),
            "emailAddress": profile.get("emailAddress"),
            "authedAt": int(time.time()),
        }
    )
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
    workers: int = typer.Option(
        4,
        "--workers",
        min=1,
        help="Number of concurrent worker threads for fetch+upload.",
    ),
    auto_prefix: bool = typer.Option(
        False,
        "--auto-prefix",
        help="Derive R2_PREFIX from the authenticated Gmail address if no prefix is explicitly set.",
    ),
    progress_every: int = typer.Option(
        200,
        "--progress-every",
        min=0,
        help="Print progress every N messages (0 disables).",
    ),
) -> None:
    _load_dotenv()
    cfg = load_app_config()
    r2 = R2Config.from_env_or_config(cfg)
    st = StateStore.open_default()
    gmail = GmailClient.from_stored_token(
        token_store=st,
        scopes=[GmailClient.SCOPE_READONLY],
    )
    r2 = _maybe_auto_prefix(r2cfg=r2, state=st, gmail=gmail, enabled=auto_prefix, explicit=_prefix_is_explicit(cfg))
    runner = BackupRunner(gmail=gmail, r2=r2, state=st)
    since_date = _parse_since(since)
    def _progress(phase: str, n: int, stats: BackupStats, elapsed_s: float) -> None:
        # Avoid noisy logs; just a periodic heartbeat.
        rate = n / elapsed_s if elapsed_s > 0 else 0.0
        print(
            f"[{phase}] processed={n} rate={rate:.1f}/s "
            f"uploaded={getattr(stats, 'uploaded', '?')} skipped={getattr(stats, 'skipped', '?')} errors={getattr(stats, 'errors', '?')}",
            file=sys.stderr,
        )

    stats = runner.run_backup(
        since=since_date,
        max_messages=max_messages,
        workers=workers,
        progress_every=progress_every,
        on_progress=_progress if progress_every else None,
    )
    print(
        "Backup complete:",
        f"uploaded={stats.uploaded}",
        f"skipped={stats.skipped}",
        f"errors={stats.errors}",
    )
    if stats.errors and getattr(stats, "error_samples", None):
        print("Sample errors:", file=sys.stderr)
        for s in stats.error_samples:
            print(f"- {s}", file=sys.stderr)
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
    workers: int = typer.Option(
        4,
        "--workers",
        min=1,
        help="Number of concurrent worker threads for restore work.",
    ),
    auto_prefix: bool = typer.Option(
        False,
        "--auto-prefix",
        help="Derive R2_PREFIX from the authenticated Gmail address if no prefix is explicitly set.",
    ),
    progress_every: int = typer.Option(
        200,
        "--progress-every",
        min=0,
        help="Print progress every N messages (0 disables).",
    ),
) -> None:
    _load_dotenv()
    cfg = load_app_config()
    r2cfg = R2Config.from_env_or_config(cfg)
    st = StateStore.open_default()
    gmail = GmailClient.from_stored_token(
        token_store=st,
        scopes=[GmailClient.SCOPE_INSERT, GmailClient.SCOPE_MODIFY],
    )
    r2cfg = _maybe_auto_prefix(r2cfg=r2cfg, state=st, gmail=gmail, enabled=auto_prefix, explicit=_prefix_is_explicit(cfg))
    r2 = R2Client(r2cfg)
    runner = RestoreRunner(gmail=gmail, r2=r2, state=st)

    since_date = _parse_since(since)
    def _progress(n: int, stats: RestoreStats, elapsed_s: float) -> None:
        rate = n / elapsed_s if elapsed_s > 0 else 0.0
        print(
            f"[restore] considered={n} rate={rate:.1f}/s "
            f"restored={getattr(stats, 'restored', '?')} skipped={getattr(stats, 'skipped', '?')} errors={getattr(stats, 'errors', '?')}",
            file=sys.stderr,
        )

    stats = runner.run_restore(
        apply=apply,
        since=since_date,
        max_messages=max_messages,
        workers=workers,
        progress_every=progress_every,
        on_progress=_progress if progress_every else None,
    )
    mode = "RESTORE" if apply else "DRY-RUN"
    print(
        f"{mode} complete:",
        f"considered={stats.considered}",
        f"restored={stats.restored}",
        f"skipped={stats.skipped}",
        f"errors={stats.errors}",
    )
    if stats.errors and getattr(stats, "error_samples", None):
        print("Sample errors:", file=sys.stderr)
        for s in stats.error_samples:
            print(f"- {s}", file=sys.stderr)
    if apply and stats.errors != 0:
        raise typer.Exit(code=2)


@app.command()
def daemon(
    every: int = typer.Option(..., "--every", min=30, help="Interval in seconds (>= 30)."),
    since: Optional[str] = typer.Option(None, "--since", help="Same as backup --since (used for fallback scans)."),
    max_messages: int = typer.Option(0, "--max-messages", min=0),
    workers: int = typer.Option(4, "--workers", min=1),
    auto_prefix: bool = typer.Option(False, "--auto-prefix"),
) -> None:
    while True:
        try:
            backup(since=since, max_messages=max_messages, workers=workers, auto_prefix=auto_prefix)
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
    _load_dotenv()
    app(prog_name="gmail-r2-backup", args=argv)


if __name__ == "__main__":
    main()
