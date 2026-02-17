from __future__ import annotations

import datetime as dt
import gzip
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .config import R2Config
from .gmail import GmailClient
from .r2 import R2Client
from .state import StateStore


@dataclass
class BackupStats:
    uploaded: int = 0
    skipped: int = 0
    errors: int = 0


class BackupRunner:
    def __init__(self, gmail: GmailClient, r2: R2Config, state: StateStore):
        self._gmail = gmail
        self._r2cfg = r2
        self._state = state
        self._r2 = R2Client(r2)

    def _gmail_query_since(self, since: dt.date) -> str:
        # Gmail query supports after:YYYY/MM/DD (interpreted in account timezone).
        return f"after:{since.strftime('%Y/%m/%d')}"

    def _upload_message(self, message_id: str) -> bool:
        if self._state.was_uploaded(message_id):
            return False

        raw, meta = self._gmail.get_message_raw(message_id)
        raw_gz = gzip.compress(raw, compresslevel=6)

        self._r2.put_bytes(f"messages/{message_id}.eml.gz", raw_gz, content_type="application/gzip")
        self._r2.put_json(f"messages/{message_id}.json", meta)
        self._state.mark_uploaded(message_id)
        return True

    def _persist_state_to_r2(self) -> None:
        st = self._state.read_state()
        self._r2.put_json("state/state.json", st)

    def _bootstrap_state_from_r2_if_needed(self) -> None:
        local = self._state.read_state()
        if local:
            return
        remote = self._r2.get_json_or_none("state/state.json")
        if remote:
            self._state.write_state(remote)

    def run_backup(
        self,
        since: dt.date | None,
        max_messages: int = 0,
        *,
        progress_every: int = 0,
        on_progress: Optional[Callable[[str, int, BackupStats, float], None]] = None,
    ) -> BackupStats:
        self._bootstrap_state_from_r2_if_needed()
        stats = BackupStats()
        started = time.monotonic()
        last_report_n = 0

        state = self._state.read_state()
        start_history_id = state.get("historyId")

        # Prefer incremental history-based backup when possible.
        used_history = False
        if start_history_id:
            try:
                for ids, latest_hid in self._gmail.history_message_added_paged(
                    start_history_id=str(start_history_id),
                    max_results=max_messages or 0,
                ):
                    used_history = True
                    for mid in ids:
                        try:
                            if self._upload_message(mid):
                                stats.uploaded += 1
                            else:
                                stats.skipped += 1
                        except Exception:
                            stats.errors += 1
                        if progress_every and on_progress:
                            n = stats.uploaded + stats.skipped + stats.errors
                            if n and (n % progress_every == 0) and n != last_report_n:
                                last_report_n = n
                                on_progress("history", n, stats, time.monotonic() - started)
                    if latest_hid:
                        self._state.write_state({"historyId": latest_hid})
            except Exception as e:
                if GmailClient.is_history_too_old(e):
                    used_history = False
                else:
                    raise

        if not used_history:
            # Fallback scan: query-based list. (Used on first run or if history is too old.)
            q = self._gmail_query_since(since) if since else None
            for mid in self._gmail.list_messages(q=q, max_results=max_messages or 0):
                try:
                    if self._upload_message(mid):
                        stats.uploaded += 1
                    else:
                        stats.skipped += 1
                except Exception:
                    stats.errors += 1
                if progress_every and on_progress:
                    n = stats.uploaded + stats.skipped + stats.errors
                    if n and (n % progress_every == 0) and n != last_report_n:
                        last_report_n = n
                        on_progress("scan", n, stats, time.monotonic() - started)

            # Update historyId to current after scan so next run can be incremental.
            profile = self._gmail.get_profile()
            if profile.get("historyId"):
                self._state.write_state({"historyId": profile.get("historyId")})

        self._state.write_state({"lastRunAt": int(time.time())})
        self._persist_state_to_r2()
        return stats
