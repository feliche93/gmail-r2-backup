from __future__ import annotations

import json
import os
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from platformdirs import user_config_dir


def _config_path() -> str:
    return os.path.join(user_config_dir("gmail-r2-backup"), "config.json")


class R2FileConfig(BaseModel):
    # Mirrors config.json shape: { "r2": { ... } }
    model_config = ConfigDict(extra="ignore")

    account_id: Optional[str] = None
    bucket: Optional[str] = None
    prefix: Optional[str] = None
    region: Optional[str] = None


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    r2: Optional[R2FileConfig] = None


def load_app_config() -> AppConfig:
    path = _config_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    # Validate but be permissive: unknown keys are ignored.
    return AppConfig.model_validate(data)


class _R2Env(BaseSettings):
    # pydantic-settings reads the environment by default; we map to the names this repo documents.
    model_config = SettingsConfigDict(extra="ignore")

    account_id: Optional[str] = Field(default=None, validation_alias="R2_ACCOUNT_ID")
    bucket: Optional[str] = Field(default=None, validation_alias="R2_BUCKET")
    prefix: Optional[str] = Field(default=None, validation_alias="R2_PREFIX")
    region: Optional[str] = Field(default=None, validation_alias="R2_REGION")


class R2Config(BaseModel):
    model_config = ConfigDict(frozen=True)

    endpoint_url: str
    bucket: str
    prefix: str
    region: str = "auto"

    @staticmethod
    def from_env_or_config(cfg: AppConfig) -> "R2Config":
        env = _R2Env()
        file_r2 = cfg.r2 or R2FileConfig()

        account_id = env.account_id or file_r2.account_id
        bucket = env.bucket or file_r2.bucket
        prefix = env.prefix or file_r2.prefix or "gmail-backup"
        region = env.region or file_r2.region or "auto"

        if not account_id:
            raise SystemExit("Missing R2_ACCOUNT_ID (or config r2.account_id)")
        if not bucket:
            raise SystemExit("Missing R2_BUCKET (or config r2.bucket)")

        endpoint_url = f"https://{account_id}.r2.cloudflarestorage.com"
        return R2Config(
            endpoint_url=endpoint_url,
            bucket=bucket,
            prefix=str(prefix).rstrip("/"),
            region=region,
        )
