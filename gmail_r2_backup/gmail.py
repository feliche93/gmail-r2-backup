from __future__ import annotations

import base64
import json
from typing import Any, Iterable

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

    @staticmethod
    def from_stored_token(token_store: StateStore, scopes: list[str]) -> "GmailClient":
        token_json = token_store.read_token_json()
        if not token_json:
            raise SystemExit("No stored token found. Run: gmail-r2-backup auth --credentials <file>")
        creds = Credentials.from_authorized_user_info(token_json, scopes=scopes)
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

    def get_profile(self) -> dict[str, Any]:
        return self._svc.users().getProfile(userId="me").execute()

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
            resp = req.execute()
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
        resp = req.execute()
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
            resp = req.execute()
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
            .execute()
        )
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
        return (
            self._svc.users()
            .messages()
            .insert(
                userId="me",
                internalDateSource=internal_date_source,
                body=body,
            )
            .execute()
        )

    def modify_labels(self, message_id: str, *, add: list[str] | None = None, remove: list[str] | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if add:
            body["addLabelIds"] = add
        if remove:
            body["removeLabelIds"] = remove
        return self._svc.users().messages().modify(userId="me", id=message_id, body=body).execute()

    def trash(self, message_id: str) -> dict[str, Any]:
        return self._svc.users().messages().trash(userId="me", id=message_id).execute()

    @staticmethod
    def is_history_too_old(err: Exception) -> bool:
        if not isinstance(err, HttpError):
            return False
        # Gmail returns 404 for invalid/too-old startHistoryId.
        return err.resp is not None and getattr(err.resp, "status", None) == 404
