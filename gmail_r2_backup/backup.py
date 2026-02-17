from __future__ import annotations

import datetime as dt
import gzip
import time
from dataclasses import dataclass
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
import threading
from dataclasses import field
from typing import Callable, Optional

from botocore.exceptions import ClientError

from .config import R2Config
from .gmail import GmailClient
from .r2 import R2Client
from .state import StateStore


@dataclass
class BackupStats:
    uploaded: int = 0
    skipped: int = 0
    errors: int = 0
    error_samples: list[str] = field(default_factory=list)


def _error_summary(exc: Exception) -> str:
    # Keep output safe/sanitized: no secrets, no message bodies.
    if isinstance(exc, ClientError):
        code = ((exc.response or {}).get("Error") or {}).get("Code")
        return f"ClientError(code={code})"
    # HttpError content may include request/response details; keep it minimal.
    if type(exc).__name__ == "HttpError":
        status = getattr(getattr(exc, "resp", None), "status", None)
        return f"HttpError(status={status})"
    return exc.__class__.__name__


class BackupRunner:
    def __init__(self, gmail: GmailClient, r2: R2Config, state: StateStore):
        self._gmail = gmail
        self._r2cfg = r2
        self._state = state
        self._r2 = R2Client(r2)
        self._worker_local = threading.local()

    def _gmail_worker(self) -> GmailClient:
        # googleapiclient service objects are not guaranteed thread-safe.
        # Use one GmailClient per worker thread.
        c = getattr(self._worker_local, "gmail", None)
        if c is None:
            c = self._gmail.clone()
            self._worker_local.gmail = c
        return c

    def _gmail_query_since(self, since: dt.date) -> str:
        # Gmail query supports after:YYYY/MM/DD (interpreted in account timezone).
        return f"after:{since.strftime('%Y/%m/%d')}"

    def _upload_message(self, message_id: str) -> bool:
        # Claim first for concurrency safety; release claim on exit.
        if not self._state.claim_upload(message_id):
            return False
        try:
            raw, meta = self._gmail_worker().get_message_raw(message_id)
            raw_gz = gzip.compress(raw, compresslevel=6)

            self._r2.put_bytes(f"messages/{message_id}.eml.gz", raw_gz, content_type="application/gzip")
            self._r2.put_json(f"messages/{message_id}.json", meta)
            self._state.mark_uploaded(message_id)
            return True
        finally:
            self._state.release_upload_claim(message_id)

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
        workers: int = 1,
        progress_every: int = 0,
        on_progress: Optional[Callable[[str, int, BackupStats, float], None]] = None,
    ) -> BackupStats:
        self._bootstrap_state_from_r2_if_needed()
        stats = BackupStats()
        started = time.monotonic()
        last_report_n = 0
        max_error_samples = 10
        listed = 0

        def record_error(mid: str, exc: Exception) -> None:
            stats.errors += 1
            if len(stats.error_samples) < max_error_samples:
                stats.error_samples.append(f"{mid}: {_error_summary(exc)}")

        state = self._state.read_state()
        start_history_id = state.get("historyId")
        full_scan_complete = bool(state.get("fullScanComplete"))

        # Prefer incremental history-based backup when possible.
        used_history = False
        if start_history_id and full_scan_complete:
            try:
                for ids, latest_hid in self._gmail.history_message_added_paged(
                    start_history_id=str(start_history_id),
                    max_results=max_messages or 0,
                ):
                    used_history = True
                    if workers <= 1:
                        mids = ids
                        for mid in mids:
                            try:
                                if self._upload_message(mid):
                                    stats.uploaded += 1
                                else:
                                    stats.skipped += 1
                            except Exception as exc:
                                record_error(mid, exc)
                            if progress_every and on_progress:
                                n = stats.uploaded + stats.skipped + stats.errors
                                if n and (n % progress_every == 0) and n != last_report_n:
                                    last_report_n = n
                                    on_progress("history", n, stats, time.monotonic() - started)
                    else:
                        with ThreadPoolExecutor(max_workers=int(workers)) as ex:
                            futs = {ex.submit(self._upload_message, mid): mid for mid in ids}
                            for fut in as_completed(futs):
                                mid = futs[fut]
                                try:
                                    if fut.result():
                                        stats.uploaded += 1
                                    else:
                                        stats.skipped += 1
                                except Exception as exc:
                                    record_error(mid, exc)
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
            if workers <= 1:
                for mid in self._gmail.list_messages(q=q, max_results=max_messages or 0):
                    listed += 1
                    try:
                        if self._upload_message(mid):
                            stats.uploaded += 1
                        else:
                            stats.skipped += 1
                    except Exception as exc:
                        record_error(mid, exc)
                    if progress_every and on_progress:
                        n = stats.uploaded + stats.skipped + stats.errors
                        if n and (n % progress_every == 0) and n != last_report_n:
                            last_report_n = n
                            on_progress("scan", n, stats, time.monotonic() - started)
            else:
                # Producer/consumer: submit as we enumerate IDs to avoid loading all IDs in memory.
                with ThreadPoolExecutor(max_workers=int(workers)) as ex:
                    pending: dict[Future[bool], str] = {}
                    for mid in self._gmail.list_messages(q=q, max_results=max_messages or 0):
                        listed += 1
                        fut = ex.submit(self._upload_message, mid)
                        pending[fut] = mid
                        if len(pending) >= int(workers) * 4:
                            done = [f for f in list(pending.keys()) if f.done()]
                            for f in done:
                                f_mid = pending.pop(f, "?")
                                try:
                                    if f.result():
                                        stats.uploaded += 1
                                    else:
                                        stats.skipped += 1
                                except Exception as exc:
                                    record_error(f_mid, exc)
                                if progress_every and on_progress:
                                    n = stats.uploaded + stats.skipped + stats.errors
                                    if n and (n % progress_every == 0) and n != last_report_n:
                                        last_report_n = n
                                        on_progress("scan", n, stats, time.monotonic() - started)
                    for f in as_completed(list(pending.keys())):
                        f_mid = pending.get(f, "?")
                        try:
                            if f.result():
                                stats.uploaded += 1
                            else:
                                stats.skipped += 1
                        except Exception as exc:
                            record_error(f_mid, exc)
                        if progress_every and on_progress:
                            n = stats.uploaded + stats.skipped + stats.errors
                            if n and (n % progress_every == 0) and n != last_report_n:
                                last_report_n = n
                                on_progress("scan", n, stats, time.monotonic() - started)

            # If the scan was capped, keep scanning on future runs (do not switch to history-based incrementals yet).
            scan_capped = bool(max_messages) and listed >= int(max_messages)
            self._state.write_state({"fullScanComplete": not scan_capped})

            if not scan_capped:
                # Update historyId to current after scan so next run can be incremental.
                profile = self._gmail.get_profile()
                if profile.get("historyId"):
                    self._state.write_state({"historyId": profile.get("historyId")})

        self._state.write_state({"lastRunAt": int(time.time())})
        self._persist_state_to_r2()
        return stats
