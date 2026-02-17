from __future__ import annotations

from types import SimpleNamespace

import pytest

import gmail_r2_backup.gmail as gmail_mod


def test_from_stored_token_uses_granted_scopes_to_avoid_invalid_scope(monkeypatch, state_store) -> None:
    state_store.write_token_json(
        {
            "token": "t",
            "refresh_token": "rt",
            "client_id": "cid",
            "client_secret": "cs",
            "token_uri": "https://oauth2.googleapis.com/token",
            # google-auth typically stores this as a space-separated string.
            "scopes": f"{gmail_mod.GmailClient.SCOPE_MODIFY} {gmail_mod.GmailClient.SCOPE_INSERT}",
        }
    )

    captured: dict[str, object] = {}

    class FakeCreds:
        expired = False
        refresh_token = None
        scopes = [gmail_mod.GmailClient.SCOPE_MODIFY, gmail_mod.GmailClient.SCOPE_INSERT]

        def refresh(self, _req) -> None:
            raise AssertionError("refresh should not be called in this test")

        def to_json(self) -> str:
            return "{}"

    def fake_from_authorized_user_info(info, scopes=None):
        captured["scopes"] = scopes
        return FakeCreds()

    monkeypatch.setattr(gmail_mod, "build", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(gmail_mod.Credentials, "from_authorized_user_info", staticmethod(fake_from_authorized_user_info))

    # Requesting a subset (readonly) should still use granted scopes for refresh compatibility.
    gmail_mod.GmailClient.from_stored_token(state_store, scopes=[gmail_mod.GmailClient.SCOPE_READONLY])
    assert captured["scopes"] == [gmail_mod.GmailClient.SCOPE_MODIFY, gmail_mod.GmailClient.SCOPE_INSERT]


def test_from_stored_token_errors_if_required_scope_missing(monkeypatch, state_store) -> None:
    state_store.write_token_json(
        {
            "token": "t",
            "refresh_token": "rt",
            "client_id": "cid",
            "client_secret": "cs",
            "token_uri": "https://oauth2.googleapis.com/token",
            "scopes": gmail_mod.GmailClient.SCOPE_READONLY,
        }
    )
    monkeypatch.setattr(gmail_mod, "build", lambda *a, **k: SimpleNamespace())
    monkeypatch.setattr(
        gmail_mod.Credentials, "from_authorized_user_info", staticmethod(lambda info, scopes=None: object())
    )

    with pytest.raises(SystemExit):
        gmail_mod.GmailClient.from_stored_token(
            state_store, scopes=[gmail_mod.GmailClient.SCOPE_INSERT, gmail_mod.GmailClient.SCOPE_MODIFY]
        )

