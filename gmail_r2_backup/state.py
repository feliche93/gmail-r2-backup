from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any

from platformdirs import user_data_dir


class StateStore:
    def __init__(self, root_dir: str):
        self.root_dir = root_dir
        self._state_path = os.path.join(root_dir, "state.json")
        self._token_path = os.path.join(root_dir, "token.json")
        self._db_path = os.path.join(root_dir, "index.sqlite3")
        os.makedirs(root_dir, exist_ok=True)
        self._init_db()

    @staticmethod
    def open_default() -> "StateStore":
        return StateStore(os.path.join(user_data_dir("gmail-r2-backup"), "state"))

    def _init_db(self) -> None:
        con = sqlite3.connect(self._db_path)
        try:
            con.execute("pragma journal_mode=WAL")
            con.execute("pragma synchronous=NORMAL")
            con.execute(
                """
                create table if not exists messages (
                  id text primary key,
                  uploaded_at integer not null
                )
                """
            )
            con.execute("create index if not exists idx_messages_uploaded_at on messages(uploaded_at)")
            con.execute(
                """
                create table if not exists inflight_uploads (
                  id text primary key,
                  claimed_at integer not null
                )
                """
            )
            con.execute("create index if not exists idx_inflight_uploads_claimed_at on inflight_uploads(claimed_at)")
            con.execute(
                """
                create table if not exists restored (
                  source_id text primary key,
                  restored_id text,
                  restored_at integer not null,
                  message_id_header text,
                  raw_sha256 text
                )
                """
            )
            con.execute("create index if not exists idx_restored_restored_at on restored(restored_at)")
            con.execute(
                """
                create table if not exists inflight_restores (
                  source_id text primary key,
                  claimed_at integer not null
                )
                """
            )
            con.execute("create index if not exists idx_inflight_restores_claimed_at on inflight_restores(claimed_at)")
            con.commit()
        finally:
            con.close()

    # ---- token storage (google-auth compatible JSON) ----
    def read_token_json(self) -> dict[str, Any] | None:
        try:
            with open(self._token_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if not isinstance(data, dict):
                    raise ValueError("token.json must be a JSON object")
                return data
        except FileNotFoundError:
            return None

    def write_token_json(self, data: dict[str, Any]) -> None:
        tmp = self._token_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, self._token_path)

    # ---- state ----
    def read_state(self) -> dict[str, Any]:
        try:
            with open(self._state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if not isinstance(data, dict):
                    raise ValueError("state.json must be a JSON object")
                return data
        except FileNotFoundError:
            return {}

    def write_state(self, patch: dict[str, Any]) -> dict[str, Any]:
        cur = self.read_state()
        cur.update(patch)
        cur["updatedAt"] = int(time.time())
        tmp = self._state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cur, f, indent=2, sort_keys=True)
        os.replace(tmp, self._state_path)
        return cur

    # ---- uploaded index ----
    def was_uploaded(self, message_id: str) -> bool:
        con = sqlite3.connect(self._db_path)
        try:
            row = con.execute("select 1 from messages where id = ?", (message_id,)).fetchone()
            return row is not None
        finally:
            con.close()

    def claim_upload(self, message_id: str, *, stale_after_s: int = 6 * 3600) -> bool:
        """
        Claims a message for upload work to avoid duplicate uploads when running concurrently.

        Returns True if the caller should proceed with the upload.
        Returns False if already uploaded or recently claimed by another worker.
        """
        now = int(time.time())
        con = sqlite3.connect(self._db_path, timeout=30)
        try:
            con.execute("pragma busy_timeout=30000")
            con.execute("begin immediate")
            row = con.execute("select 1 from messages where id = ?", (message_id,)).fetchone()
            if row is not None:
                con.execute("commit")
                return False

            cur = con.execute(
                "insert into inflight_uploads(id, claimed_at) values(?, ?) on conflict(id) do nothing",
                (message_id, now),
            )
            if cur.rowcount == 1:
                con.execute("commit")
                return True

            # Existing claim: allow reclaim if stale.
            row = con.execute("select claimed_at from inflight_uploads where id = ?", (message_id,)).fetchone()
            claimed_at = int(row[0]) if row else 0
            if claimed_at and (now - claimed_at) > stale_after_s:
                con.execute("update inflight_uploads set claimed_at = ? where id = ?", (now, message_id))
                con.execute("commit")
                return True

            con.execute("commit")
            return False
        finally:
            con.close()

    def release_upload_claim(self, message_id: str) -> None:
        con = sqlite3.connect(self._db_path, timeout=30)
        try:
            con.execute("pragma busy_timeout=30000")
            con.execute("delete from inflight_uploads where id = ?", (message_id,))
            con.commit()
        finally:
            con.close()

    def mark_uploaded(self, message_id: str) -> None:
        con = sqlite3.connect(self._db_path)
        try:
            con.execute(
                "insert into messages(id, uploaded_at) values(?, ?) on conflict(id) do nothing",
                (message_id, int(time.time())),
            )
            con.commit()
        finally:
            con.close()

    # ---- restore index ----
    def was_restored(self, source_message_id: str) -> bool:
        con = sqlite3.connect(self._db_path)
        try:
            row = con.execute("select 1 from restored where source_id = ?", (source_message_id,)).fetchone()
            return row is not None
        finally:
            con.close()

    def claim_restore(self, source_message_id: str, *, stale_after_s: int = 6 * 3600) -> bool:
        """
        Claims a message for restore work to avoid duplicates when running concurrently.
        """
        now = int(time.time())
        con = sqlite3.connect(self._db_path, timeout=30)
        try:
            con.execute("pragma busy_timeout=30000")
            con.execute("begin immediate")
            row = con.execute("select 1 from restored where source_id = ?", (source_message_id,)).fetchone()
            if row is not None:
                con.execute("commit")
                return False

            cur = con.execute(
                "insert into inflight_restores(source_id, claimed_at) values(?, ?) on conflict(source_id) do nothing",
                (source_message_id, now),
            )
            if cur.rowcount == 1:
                con.execute("commit")
                return True

            row = con.execute(
                "select claimed_at from inflight_restores where source_id = ?", (source_message_id,)
            ).fetchone()
            claimed_at = int(row[0]) if row else 0
            if claimed_at and (now - claimed_at) > stale_after_s:
                con.execute("update inflight_restores set claimed_at = ? where source_id = ?", (now, source_message_id))
                con.execute("commit")
                return True

            con.execute("commit")
            return False
        finally:
            con.close()

    def release_restore_claim(self, source_message_id: str) -> None:
        con = sqlite3.connect(self._db_path, timeout=30)
        try:
            con.execute("pragma busy_timeout=30000")
            con.execute("delete from inflight_restores where source_id = ?", (source_message_id,))
            con.commit()
        finally:
            con.close()

    def mark_restored(
        self,
        *,
        source_message_id: str,
        restored_message_id: str | None,
        message_id_header: str | None,
        raw_sha256: str | None,
    ) -> None:
        con = sqlite3.connect(self._db_path)
        try:
            con.execute(
                """
                insert into restored(source_id, restored_id, restored_at, message_id_header, raw_sha256)
                values(?, ?, ?, ?, ?)
                on conflict(source_id) do update set
                  restored_id=excluded.restored_id,
                  restored_at=excluded.restored_at,
                  message_id_header=excluded.message_id_header,
                  raw_sha256=excluded.raw_sha256
                """,
                (
                    source_message_id,
                    restored_message_id,
                    int(time.time()),
                    message_id_header,
                    raw_sha256,
                ),
            )
            con.commit()
        finally:
            con.close()
