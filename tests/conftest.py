from __future__ import annotations

import pytest

from gmail_r2_backup.state import StateStore


@pytest.fixture()
def state_store(tmp_path) -> StateStore:
    return StateStore(str(tmp_path / "state"))

