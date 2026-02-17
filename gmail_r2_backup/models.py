from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class MessageMeta(BaseModel):
    # Stored alongside the raw .eml.gz in R2.
    model_config = ConfigDict(extra="ignore")

    id: Optional[str] = None
    threadId: Optional[str] = None
    labelIds: Optional[list[str]] = None
    internalDate: Optional[str] = None
    sizeEstimate: Optional[int] = None
    historyId: Optional[str] = None

    def label_ids(self) -> list[str]:
        return list(self.labelIds or [])


def parse_message_meta(obj: Any) -> MessageMeta:
    return MessageMeta.model_validate(obj)

