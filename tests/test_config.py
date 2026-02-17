from __future__ import annotations

import pytest

from gmail_r2_backup.config import AppConfig, R2Config, R2FileConfig


def test_r2_config_prefers_env_over_file(monkeypatch) -> None:
    cfg = AppConfig(r2=R2FileConfig(account_id="file", bucket="file-bucket", prefix="file-prefix/", region="auto"))
    monkeypatch.setenv("R2_ACCOUNT_ID", "env")
    monkeypatch.setenv("R2_BUCKET", "env-bucket")
    monkeypatch.setenv("R2_PREFIX", "env-prefix/")
    monkeypatch.setenv("R2_REGION", "auto")

    r2 = R2Config.from_env_or_config(cfg)
    assert r2.endpoint_url == "https://env.r2.cloudflarestorage.com"
    assert r2.bucket == "env-bucket"
    assert r2.prefix == "env-prefix"


def test_r2_config_falls_back_to_file(monkeypatch) -> None:
    monkeypatch.delenv("R2_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("R2_BUCKET", raising=False)
    monkeypatch.delenv("R2_PREFIX", raising=False)
    monkeypatch.delenv("R2_REGION", raising=False)

    cfg = AppConfig(r2=R2FileConfig(account_id="file", bucket="file-bucket", prefix="p/", region="auto"))
    r2 = R2Config.from_env_or_config(cfg)
    assert r2.bucket == "file-bucket"
    assert r2.prefix == "p"


def test_r2_config_requires_account_and_bucket(monkeypatch) -> None:
    monkeypatch.delenv("R2_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("R2_BUCKET", raising=False)
    cfg = AppConfig(r2=R2FileConfig())

    with pytest.raises(SystemExit):
        _ = R2Config.from_env_or_config(cfg)

