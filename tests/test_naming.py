from __future__ import annotations

from gmail_r2_backup.naming import r2_prefix_from_email


def test_r2_prefix_from_email_basic() -> None:
    assert r2_prefix_from_email("User.Name+tag@gmail.com") == "gmail-backup/user.name-tag-at-gmail.com"


def test_r2_prefix_from_email_empty() -> None:
    assert r2_prefix_from_email("") == "gmail-backup/gmail"

