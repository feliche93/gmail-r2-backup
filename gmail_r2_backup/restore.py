from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import re
import time
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from typing import Callable, Optional

from googleapiclient.errors import HttpError

from .gmail import GmailClient
from .models import parse_message_meta
from .r2 import R2Client
from .state import StateStore


@dataclass
class RestoreStats:
    considered: int = 0
    restored: int = 0
    skipped: int = 0
    errors: int = 0


_MSGID_CLEAN = re.compile(r"^<(.+)>$")


def _extract_message_id_header(raw_bytes: bytes) -> Optional[str]:
    # Parse only headers; raw payload can be large.
    msg = BytesParser(policy=policy.default).parsebytes(raw_bytes, headersonly=True)
    v = msg.get("Message-ID")
    if not v:
        return None
    v = str(v).strip()
    m = _MSGID_CLEAN.match(v)
    if m:
        v = m.group(1)
    return v or None


def _sha256(raw_bytes: bytes) -> str:
    return hashlib.sha256(raw_bytes).hexdigest()


class RestoreRunner:
    def __init__(self, *, gmail: GmailClient, r2: R2Client, state: StateStore):
        self._gmail = gmail
        self._r2 = r2
        self._state = state

    def _iter_backed_up_message_ids(self) -> list[str]:
        # Each message has messages/<id>.eml.gz and messages/<id>.json
        keys = self._r2.list_keys("messages/")
        out: list[str] = []
        for k in keys:
            if not k.startswith("messages/"):
                continue
            if not k.endswith(".eml.gz"):
                continue
            base = k[len("messages/") : -len(".eml.gz")]
            if base:
                out.append(base)
        out.sort()
        return out

    def _is_present_in_gmail_by_msgid(self, msgid: str) -> bool:
        # Gmail search operator: rfc822msgid:
        # This is the best available stable dedupe key for restores.
        q = f"rfc822msgid:{msgid}"
        for _mid in self._gmail.search_message_ids(q=q, max_results=1):
            return True
        return False

    def _restore_one(self, source_id: str, *, apply: bool) -> tuple[str | None, str | None, str | None, bool]:
        """
        Returns (restored_message_id, message_id_header, raw_sha256, did_restore)
        """
        if self._state.was_restored(source_id):
            return None, None, None, False

        raw_gz = self._r2.get_bytes(f"messages/{source_id}.eml.gz")
        raw_bytes = gzip.decompress(raw_gz)
        raw_hash = _sha256(raw_bytes)

        meta_obj = self._r2.get_json_or_none(f"messages/{source_id}.json") or {}
        meta = parse_message_meta(meta_obj)
        label_ids = meta.label_ids()

        msgid = _extract_message_id_header(raw_bytes)
        if msgid and self._is_present_in_gmail_by_msgid(msgid):
            self._state.mark_restored(
                source_message_id=source_id,
                restored_message_id=None,
                message_id_header=msgid,
                raw_sha256=raw_hash,
            )
            return None, msgid, raw_hash, False

        if not apply:
            return None, msgid, raw_hash, True

        restored_id: str | None = None
        try:
            inserted = self._gmail.insert_message_raw(
                raw_bytes,
                label_ids=label_ids or None,
                internal_date_source="dateHeader",
            )
            restored_id = inserted.get("id")
        except HttpError as e:
            # Some system labels can cause insert failures. Retry without labelIds,
            # then re-apply what we can via modify/trash.
            if getattr(getattr(e, "resp", None), "status", None) not in (400, 403):
                raise
            inserted = self._gmail.insert_message_raw(
                raw_bytes,
                label_ids=None,
                internal_date_source="dateHeader",
            )
            restored_id = inserted.get("id")

        if restored_id:
            # Best-effort re-apply labels and special locations.
            # Note: Some system labels may be restricted; failures are ignored per "skip silently".
            try:
                if label_ids:
                    self._gmail.modify_labels(restored_id, add=label_ids)
            except Exception:
                pass
            try:
                if "TRASH" in (label_ids or []):
                    self._gmail.trash(restored_id)
            except Exception:
                pass
            try:
                if "SPAM" in (label_ids or []):
                    self._gmail.modify_labels(restored_id, add=["SPAM"])
            except Exception:
                pass

        self._state.mark_restored(
            source_message_id=source_id,
            restored_message_id=restored_id,
            message_id_header=msgid,
            raw_sha256=raw_hash,
        )
        return restored_id, msgid, raw_hash, True

    def run_restore(
        self,
        *,
        apply: bool,
        since: dt.date | None = None,
        max_messages: int = 0,
        progress_every: int = 0,
        on_progress: Optional[Callable[[int, RestoreStats, float], None]] = None,
    ) -> RestoreStats:
        stats = RestoreStats()
        ids = self._iter_backed_up_message_ids()
        started = time.monotonic()
        last_report_n = 0

        for source_id in ids:
            if max_messages and stats.considered >= max_messages:
                break

            # Optional filter: compare against backed-up internalDate if present (ms since epoch).
            if since:
                meta_obj = self._r2.get_json_or_none(f"messages/{source_id}.json") or {}
                meta = parse_message_meta(meta_obj)
                try:
                    if meta.internalDate:
                        ts_ms = int(meta.internalDate)
                        d = dt.datetime.fromtimestamp(ts_ms / 1000.0, tz=dt.timezone.utc).date()
                        if d < since:
                            continue
                except Exception:
                    pass

            stats.considered += 1
            try:
                _restored_id, _msgid, _raw_hash, did_restore = self._restore_one(source_id, apply=apply)
                if did_restore:
                    if apply:
                        stats.restored += 1
                    else:
                        # dry-run counts this as "would restore"
                        stats.restored += 1
                else:
                    stats.skipped += 1
            except Exception:
                stats.errors += 1

            if progress_every and on_progress:
                n = stats.considered
                if n and (n % progress_every == 0) and n != last_report_n:
                    last_report_n = n
                    on_progress(n, stats, time.monotonic() - started)

        return stats
