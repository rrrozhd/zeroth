from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class DecisionReportCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DecisionReportOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_id: str
    decision_id: str
    template_version: str
    media_type: str
    sha256: str
    download_path: str
    created_at: datetime


class DecisionReportDeliveryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipients: list[EmailStr] = Field(min_length=1, max_length=20)
    delivery_mode: Literal["attachment", "link"] = "link"


class DecisionReportDeliveryOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    delivery_id: str
    report_id: str
    report_sha256: str
    recipients: list[EmailStr]
    delivery_mode: Literal["attachment", "link"]
    status: Literal["sent", "failed"]
    last_error: str | None
    created_at: datetime
    sent_at: datetime | None
