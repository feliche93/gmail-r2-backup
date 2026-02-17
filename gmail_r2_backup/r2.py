from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from .config import R2Config


@dataclass(frozen=True)
class PutResult:
    etag: str | None


class R2Client:
    def __init__(self, cfg: R2Config):
        # R2 is S3-compatible; boto3 uses AWS_* env vars for credentials.
        self._cfg = cfg
        self._s3 = boto3.client(
            "s3",
            region_name=cfg.region,
            endpoint_url=cfg.endpoint_url,
            config=BotoConfig(
                retries={"max_attempts": 10, "mode": "standard"},
                s3={"addressing_style": "path"},
            ),
        )

    def _key(self, key: str) -> str:
        key = key.lstrip("/")
        if not self._cfg.prefix:
            return key
        return f"{self._cfg.prefix}/{key}"

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> PutResult:
        extra: dict[str, Any] = {}
        if content_type:
            extra["ContentType"] = content_type
        resp = self._s3.put_object(Bucket=self._cfg.bucket, Key=self._key(key), Body=data, **extra)
        return PutResult(etag=resp.get("ETag"))

    def put_json(self, key: str, obj: Any) -> PutResult:
        data = json.dumps(obj, indent=2, sort_keys=True).encode("utf-8")
        return self.put_bytes(key, data, content_type="application/json")

    def get_bytes(self, key: str) -> bytes:
        resp = self._s3.get_object(Bucket=self._cfg.bucket, Key=self._key(key))
        return resp["Body"].read()

    def get_json_or_none(self, key: str) -> Any | None:
        try:
            resp = self._s3.get_object(Bucket=self._cfg.bucket, Key=self._key(key))
        except ClientError as e:
            code = (e.response or {}).get("Error", {}).get("Code")
            if code in ("NoSuchKey", "404"):
                return None
            raise
        body = resp["Body"].read()
        return json.loads(body.decode("utf-8"))

    def list_keys(self, key_prefix: str) -> list[str]:
        # Returns keys relative to the configured prefix (i.e. without cfg.prefix/).
        # key_prefix is also relative (e.g. "messages/").
        prefix = self._key(key_prefix)
        out: list[str] = []
        token = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": self._cfg.bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            resp = self._s3.list_objects_v2(**kwargs)
            for obj in resp.get("Contents", []) or []:
                k = obj.get("Key")
                if not k:
                    continue
                # Strip configured prefix to return relative keys.
                if self._cfg.prefix and k.startswith(self._cfg.prefix.rstrip("/") + "/"):
                    k = k[len(self._cfg.prefix.rstrip("/") + "/") :]
                out.append(k)
            if resp.get("IsTruncated"):
                token = resp.get("NextContinuationToken")
                continue
            return out
