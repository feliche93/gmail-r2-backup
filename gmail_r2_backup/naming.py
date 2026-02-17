from __future__ import annotations

import re


_NON_SAFE = re.compile(r"[^a-z0-9._-]+")
_MULTI_DASH = re.compile(r"-{2,}")


def r2_prefix_from_email(email_address: str) -> str:
    """
    Derive a stable, path-safe R2 prefix from a Gmail email address.

    Note: this may embed the email (sanitized) into object keys; only enable when desired.
    """
    s = (email_address or "").strip().lower()
    s = s.replace("@", "-at-")
    s = _NON_SAFE.sub("-", s)
    s = _MULTI_DASH.sub("-", s).strip("-")
    if not s:
        s = "gmail"
    return f"gmail-backup/{s}"

