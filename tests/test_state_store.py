from __future__ import annotations

from gmail_r2_backup import state as state_mod


def test_claim_upload_idempotent(state_store, monkeypatch) -> None:
    monkeypatch.setattr(state_mod.time, "time", lambda: 1000)

    assert state_store.claim_upload("m1") is True
    assert state_store.claim_upload("m1") is False

    state_store.mark_uploaded("m1")
    assert state_store.claim_upload("m1") is False


def test_claim_upload_reclaims_when_stale(state_store, monkeypatch) -> None:
    monkeypatch.setattr(state_mod.time, "time", lambda: 1000)
    assert state_store.claim_upload("m1", stale_after_s=3600) is True

    monkeypatch.setattr(state_mod.time, "time", lambda: 1001)
    assert state_store.claim_upload("m1", stale_after_s=3600) is False

    monkeypatch.setattr(state_mod.time, "time", lambda: 1000 + 3600 + 1)
    assert state_store.claim_upload("m1", stale_after_s=3600) is True


def test_claim_restore_idempotent(state_store, monkeypatch) -> None:
    monkeypatch.setattr(state_mod.time, "time", lambda: 1000)

    assert state_store.claim_restore("s1") is True
    assert state_store.claim_restore("s1") is False

    state_store.mark_restored(source_message_id="s1", restored_message_id="r1", message_id_header=None, raw_sha256=None)
    assert state_store.claim_restore("s1") is False

