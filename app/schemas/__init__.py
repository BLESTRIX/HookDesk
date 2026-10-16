from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

Name = Annotated[
    str,
    StringConstraints(min_length=1, max_length=120, strip_whitespace=True, pattern=r"^[^\x00]+$"),
]


def postgres_text(value):
    if "\x00" in value:
        raise ValueError("Null characters are not allowed")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError("Use valid Unicode text") from None
    return value


class RegisterInput(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)
    organization_name: Name


class LoginInput(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)
    organization_id: str | None = None

    @field_validator("organization_id", mode="before")
    @classmethod
    def empty_org(cls, value):
        return value or None


class SourceInput(BaseModel):
    name: Name


class SourcePatch(BaseModel):
    name: Name | None = None
    active: bool | None = None


class RotationInput(BaseModel):
    grace_seconds: int | None = Field(default=None, ge=0, le=604800)


class ReplayInput(BaseModel):
    reason: str = Field(min_length=3, max_length=500)

    @field_validator("reason")
    @classmethod
    def nonblank(cls, value):
        if len(value.strip()) < 3:
            raise ValueError("A meaningful replay reason is required")
        return postgres_text(value.strip())


class ShipmentData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shipment_id: str = Field(min_length=1, max_length=100)
    sequence: int = Field(gt=0, strict=True)
    status: Literal["shipped", "delivered"]

    @field_validator("shipment_id")
    @classmethod
    def valid_identifier(cls, value):
        return postgres_text(value)


class CourierEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(min_length=1, max_length=128)
    event_type: Literal["shipment.status_changed"]
    occurred_at: datetime
    data: ShipmentData

    @field_validator("event_id")
    @classmethod
    def valid_identifier(cls, value):
        return postgres_text(value)

    @field_validator("occurred_at", mode="before")
    @classmethod
    def iso_string(cls, value):
        if not isinstance(value, str):
            raise ValueError("occurred_at must be an ISO timestamp string")
        return datetime.fromisoformat(value)

    @field_validator("occurred_at")
    @classmethod
    def aware_timestamp(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone")
        return value


class EventFilters(BaseModel):
    source_id: str | None = None
    event_type: Literal["shipment.status_changed"] | None = None
    state: (
        Literal["QUEUED", "PROCESSING", "RETRY_WAIT", "SUCCEEDED", "IGNORED", "FAILED"] | None
    ) = None
    received_from: datetime | None = None
    received_to: datetime | None = None
    external_event_id: str | None = Field(default=None, max_length=128)
    shipment_id: str | None = Field(default=None, max_length=100)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=25, ge=1, le=100)

    @field_validator("source_id")
    @classmethod
    def uuid_source(cls, value):
        return str(UUID(value)) if value else None

    @model_validator(mode="after")
    def ordered_dates(self):
        if self.received_from and self.received_to and self.received_from > self.received_to:
            raise ValueError("Received-from must be earlier than received-to")
        return self

    @field_validator("received_from", "received_to")
    @classmethod
    def aware_filter(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError("Date filters must include a timezone")
        return value


class ErrorResponse(BaseModel):
    code: str
    message: str
    request_id: str


class Page(BaseModel):
    items: list[dict]
    total: int
    page: int
    page_size: int
