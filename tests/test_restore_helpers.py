from __future__ import annotations

from gmail_r2_backup.restore import _extract_message_id_header


def test_extract_message_id_header() -> None:
    raw = b"From: a@example.com\r\nMessage-ID: <abc123@example.com>\r\n\r\nBody"
    assert _extract_message_id_header(raw) == "abc123@example.com"


def test_extract_message_id_header_without_brackets() -> None:
    raw = b"From: a@example.com\r\nMessage-ID: abc123@example.com\r\n\r\nBody"
    assert _extract_message_id_header(raw) == "abc123@example.com"


def test_extract_message_id_header_missing() -> None:
    raw = b"From: a@example.com\r\nSubject: hi\r\n\r\nBody"
    assert _extract_message_id_header(raw) is None

