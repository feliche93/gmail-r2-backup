from __future__ import annotations

import base64
import json
import random
import time
from typing import Any, Iterable, Optional, cast

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .state import StateStore


class GmailClient:
    SCOPE_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
    SCOPE_INSERT = "https://www.googleapis.com/auth/gmail.insert"
    SCOPE_MODIFY = "https://www.googleapis.com/auth/gmail.modify"

    def __init__(self, creds: Credentials):
        self._creds = creds
        self._svc = build("gmail", "v1", credentials=creds, cache_discovery=False)

    def clone(self) -> "GmailClient":
        """
        Create a new GmailClient with independent underlying HTTP/service objects.

        This avoids sharing googleapiclient Resource instances across threads.
        """
        info = json.loads(self._creds.to_json())
        scopes = list(self._creds.scopes) if getattr(self._creds, "scopes", None) else None
        creds = Credentials.from_authorized_user_info(info, scopes=scopes)
        return GmailClient(creds)

    @staticmethod
    def _error_reason(err: HttpError) -> str | None:
        # Best-effort parse of Google API error payload.
        try:
            raw = getattr(err, "content", None)
            if not raw:
                return None
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            data = json.loads(raw)
            error = (data or {}).get("error") or {}
            errors = error.get("errors") or []
            if errors and isinstance(errors, list):
                reason = (errors[0] or {}).get("reason")
                if isinstance(reason, str):
                    return reason
            status = error.get("status")
            if isinstance(status, str):
                return status
        except Exception:
            return None
        return None

    @classmethod
    def _should_retry(cls, err: HttpError) -> bool:
        status = getattr(getattr(err, "resp", None), "status", None)
        if status in (429, 500, 502, 503, 504):
            return True
        if status == 403:
            reason = cls._error_reason(err)
            if reason in ("rateLimitExceeded", "userRateLimitExceeded", "backendError"):
                return True
        return False

    @classmethod
    def _execute_with_retries(cls, req: Any, *, max_attempts: int = 8) -> Any:
        delay_s = 1.0
        for attempt in range(1, max_attempts + 1):
            try:
                return req.execute()
            except HttpError as e:
                if attempt >= max_attempts or not cls._should_retry(e):
                    raise
                # Exponential backoff with jitter, capped.
                sleep_s = delay_s * (0.5 + random.random())
                time.sleep(min(sleep_s, 60.0))
                delay_s = min(delay_s * 2.0, 60.0)

    @staticmethod
    def _normalize_scopes(v: Any) -> list[str] | None:
        if v is None:
            return None
        if isinstance(v, str):
            parts = [p.strip() for p in v.split() if p.strip()]
            return parts or None
        if isinstance(v, list):
            out: list[str] = []
            for x in v:
                if isinstance(x, str) and x.strip():
                    out.append(x.strip())
            return out or None
        return None

    @classmethod
    def _satisfies_required_scopes(cls, granted: set[str], required: list[str]) -> bool:
        # gmail.modify implies read access, and is generally sufficient for insert as well.
        # Keep the mapping minimal and conservative; treat https://mail.google.com/ as full access.
        full = "https://mail.google.com/"
        for req in required:
            if req == cls.SCOPE_READONLY:
                if not (req in granted or cls.SCOPE_MODIFY in granted or full in granted):
                    return False
            elif req == cls.SCOPE_INSERT:
                if not (req in granted or cls.SCOPE_MODIFY in granted or full in granted):
                    return False
            elif req == cls.SCOPE_MODIFY:
                if not (req in granted or full in granted):
                    return False
            else:
                if not (req in granted or full in granted):
                    return False
        return True

    @staticmethod
    def from_stored_token(token_store: StateStore, scopes: list[str]) -> "GmailClient":
        token_json = token_store.read_token_json()
        if not token_json:
            raise SystemExit(
                "No stored token found. Run: gmail-r2-backup auth --credentials <file> "
                "(or use --client-id/--client-secret)."
            )

        # IMPORTANT:
        # The refresh token is bound to the originally granted scopes. Passing a different
        # scope set here can cause refresh to fail with "invalid_scope".
        granted = GmailClient._normalize_scopes(token_json.get("scopes")) if isinstance(token_json, dict) else None
        if granted:
            if not GmailClient._satisfies_required_scopes(set(granted), scopes):
                raise SystemExit(
                    "Stored token is missing required scopes for this command. "
                    "Re-run auth with the right scopes (e.g. `gmail-r2-backup auth --write ...`)."
                )
            effective_scopes = granted
        else:
            effective_scopes = scopes

        creds = Credentials.from_authorized_user_info(token_json, scopes=effective_scopes)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_store.write_token_json(json.loads(creds.to_json()))
        return GmailClient(creds)

    @staticmethod
    def from_oauth_desktop_flow(
        credentials_path: str,
        token_store: StateStore,
        scopes: list[str],
    ) -> "GmailClient":
        flow = InstalledAppFlow.from_client_secrets_file(credentials_path, scopes=scopes)
        creds = flow.run_local_server(port=0)
        token_store.write_token_json(json.loads(creds.to_json()))
        return GmailClient(creds)

    @staticmethod
    def from_oauth_desktop_flow_client_secrets(
        *,
        client_id: str,
        client_secret: str,
        token_store: StateStore,
        scopes: list[str],
    ) -> "GmailClient":
        # Equivalent to using a downloaded "Desktop app" client JSON, but lets users provide
        # client_id/client_secret via env vars instead of a file.
        cfg = {
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        }
        flow = InstalledAppFlow.from_client_config(cfg, scopes=scopes)
        creds = flow.run_local_server(port=0)
        token_store.write_token_json(json.loads(creds.to_json()))
        return GmailClient(creds)

    def get_profile(self) -> dict[str, Any]:
        req = self._svc.users().getProfile(userId="me")
        return cast(dict[str, Any], self._execute_with_retries(req))

    def list_messages(self, q: str | None = None, max_results: int = 0) -> Iterable[str]:
        # Yields message IDs
        page_token = None
        yielded = 0
        while True:
            req = self._svc.users().messages().list(
                userId="me",
                q=q,
                pageToken=page_token,
                maxResults=500,
                includeSpamTrash=True,
            )
            resp = self._execute_with_retries(req)
            for m in resp.get("messages", []) or []:
                mid = m.get("id")
                if not mid:
                    continue
                yield mid
                yielded += 1
                if max_results and yielded >= max_results:
                    return
            page_token = resp.get("nextPageToken")
            if not page_token:
                return

    def history_message_added(self, start_history_id: str, max_results: int = 0) -> tuple[list[str], str | None, str | None]:
        # Returns (message_ids, latest_history_id, next_page_token)
        req = self._svc.users().history().list(
            userId="me",
            startHistoryId=start_history_id,
            historyTypes=["messageAdded"],
            maxResults=500,
        )
        resp = self._execute_with_retries(req)
        ids: list[str] = []
        for h in resp.get("history", []) or []:
            for added in h.get("messagesAdded", []) or []:
                msg = added.get("message") or {}
                mid = msg.get("id")
                if mid:
                    ids.append(mid)
                    if max_results and len(ids) >= max_results:
                        break
            if max_results and len(ids) >= max_results:
                break
        return ids, resp.get("historyId"), resp.get("nextPageToken")

    def history_message_added_paged(self, start_history_id: str, max_results: int = 0) -> Iterable[tuple[list[str], str | None]]:
        # Yields (message_ids, latest_history_id) per page
        page_token = None
        yielded = 0
        while True:
            req = self._svc.users().history().list(
                userId="me",
                startHistoryId=start_history_id,
                historyTypes=["messageAdded"],
                maxResults=500,
                pageToken=page_token,
            )
            resp = self._execute_with_retries(req)
            ids: list[str] = []
            for h in resp.get("history", []) or []:
                for added in h.get("messagesAdded", []) or []:
                    msg = added.get("message") or {}
                    mid = msg.get("id")
                    if mid:
                        ids.append(mid)
                        yielded += 1
                        if max_results and yielded >= max_results:
                            break
                if max_results and yielded >= max_results:
                    break
            yield ids, resp.get("historyId")
            page_token = resp.get("nextPageToken")
            if not page_token or (max_results and yielded >= max_results):
                return

    def get_message_raw(self, message_id: str) -> tuple[bytes, dict[str, Any]]:
        msg = (
            self._svc.users()
            .messages()
            .get(userId="me", id=message_id, format="raw")
        )
        msg = self._execute_with_retries(msg)
        msg = cast(dict[str, Any], msg)
        raw_b64 = msg.get("raw")
        if not raw_b64:
            raise ValueError("No raw content for message")
        raw_bytes = base64.urlsafe_b64decode(raw_b64.encode("ascii"))
        meta = {
            "id": msg.get("id"),
            "threadId": msg.get("threadId"),
            "labelIds": msg.get("labelIds"),
            "internalDate": msg.get("internalDate"),
            "sizeEstimate": msg.get("sizeEstimate"),
            "historyId": msg.get("historyId"),
        }
        return raw_bytes, meta

    def search_message_ids(self, q: str, max_results: int = 0) -> Iterable[str]:
        # Convenience wrapper for dedupe queries during restore.
        return self.list_messages(q=q, max_results=max_results)

    def insert_message_raw(
        self,
        raw_bytes: bytes,
        *,
        label_ids: list[str] | None = None,
        internal_date_source: str = "dateHeader",
    ) -> dict[str, Any]:
        # See: users.messages.insert
        raw_b64 = base64.urlsafe_b64encode(raw_bytes).decode("ascii")
        body: dict[str, Any] = {"raw": raw_b64}
        if label_ids:
            body["labelIds"] = label_ids
        req = (
            self._svc.users()
            .messages()
            .insert(
                userId="me",
                internalDateSource=internal_date_source,
                body=body,
            )
        )
        return cast(dict[str, Any], self._execute_with_retries(req))

    def modify_labels(self, message_id: str, *, add: list[str] | None = None, remove: list[str] | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if add:
            body["addLabelIds"] = add
        if remove:
            body["removeLabelIds"] = remove
        req = self._svc.users().messages().modify(userId="me", id=message_id, body=body)
        return cast(dict[str, Any], self._execute_with_retries(req))

    def trash(self, message_id: str) -> dict[str, Any]:
        req = self._svc.users().messages().trash(userId="me", id=message_id)
        return cast(dict[str, Any], self._execute_with_retries(req))

    @staticmethod
    def is_history_too_old(err: Exception) -> bool:
        if not isinstance(err, HttpError):
            return False
        # Gmail returns 404 for invalid/too-old startHistoryId.
        return err.resp is not None and getattr(err.resp, "status", None) == 404
