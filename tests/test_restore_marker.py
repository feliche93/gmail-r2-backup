from __future__ import annotations

from gmail_r2_backup.restore import RestoreRunner


class _FakeR2:
    def __init__(self, marker: dict | None):
        self._marker = marker

    def get_json_or_none(self, key: str):
        assert key.startswith("state/restore/")
        return self._marker


class _FakeGmail:
    # Not used in this test path.
    def clone(self):
        return self


def test_restore_skips_when_marker_present(state_store) -> None:
    marker = {"status": "inserted", "sourceId": "m1", "restoredId": "x", "rawSha256": "h", "messageIdHeader": "mid"}
    runner = RestoreRunner(gmail=_FakeGmail(), r2=_FakeR2(marker), state=state_store)  # type: ignore[arg-type]

    restored_id, msgid, raw_hash, did_restore = runner._restore_one("m1", apply=True)  # noqa: SLF001
    assert did_restore is False
    assert restored_id is None
    assert msgid is None
    assert raw_hash is None
    assert state_store.was_restored("m1") is True

