"""Data models shared by the API, storage layer and monitor."""

from __future__ import annotations

import re
from datetime import datetime
from email.utils import parseaddr
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SourceType = Literal["protect", "rtsp", "http"]
Metric = Literal["peak", "mean"]
LampStateName = Literal["on", "off", "unknown"]

SECRET_FIELDS = frozenset({"protect_api_key", "smtp_password"})

_ADDRESS_RE = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")


def parse_addresses(value: str) -> list[str]:
    """Split a comma, semicolon or whitespace separated list of email addresses."""
    return [part for part in re.split(r"[,;\s]+", value or "") if part]


def _normalize_addresses(value: str) -> str:
    addresses = parse_addresses(value)
    invalid = [a for a in addresses if not _ADDRESS_RE.match(a)]
    if invalid:
        raise ValueError(f"not a valid email address: {', '.join(invalid)}")
    return ", ".join(addresses)


def _strip(value: Any) -> Any:
    return value.strip() if isinstance(value, str) else value


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CameraIn(_Model):
    name: str = Field(min_length=1, max_length=100)
    source_type: SourceType
    protect_camera_id: str = ""
    url: str = ""
    poll_interval: float = Field(5.0, ge=1, le=3600)
    enabled: bool = True

    _strip_text = field_validator("name", "protect_camera_id", "url", mode="before")(_strip)

    @model_validator(mode="after")
    def _check_source(self) -> CameraIn:
        if self.source_type == "protect" and not self.protect_camera_id:
            raise ValueError("choose a UniFi Protect camera")
        if self.source_type == "rtsp" and not self.url.lower().startswith(("rtsp://", "rtsps://")):
            raise ValueError("stream URL must start with rtsp:// or rtsps://")
        if self.source_type == "http" and not self.url.lower().startswith(("http://", "https://")):
            raise ValueError("snapshot URL must start with http:// or https://")
        return self


class Camera(CameraIn):
    id: int


class Region(_Model):
    """A rectangle in normalized image coordinates (0..1, origin top-left)."""

    x: float = Field(ge=0, lt=1)
    y: float = Field(ge=0, lt=1)
    w: float = Field(gt=0, le=1)
    h: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def _fit_inside_image(self) -> Region:
        if self.x + self.w > 1.001 or self.y + self.h > 1.001:
            raise ValueError("region must lie within the image")
        self.w = min(self.w, 1 - self.x)
        self.h = min(self.h, 1 - self.y)
        return self


class MeasureRequest(Region):
    metric: Metric = "peak"


class LampIn(Region):
    name: str = Field(min_length=1, max_length=100)
    metric: Metric = "peak"
    threshold: float = Field(128, ge=0, le=255)
    hysteresis: float = Field(10, ge=0, le=255)
    confirm_seconds: float = Field(15, ge=0, le=86400)
    email_to: str = ""
    notes: str = Field("", max_length=2000)
    notify_on_restore: bool = True
    repeat_minutes: float = Field(0, ge=0, le=10080)
    enabled: bool = True

    _strip_text = field_validator("name", "notes", mode="before")(_strip)

    @field_validator("email_to")
    @classmethod
    def _check_email_to(cls, value: str) -> str:
        return _normalize_addresses(value)


class Lamp(LampIn):
    id: int
    camera_id: int
    state: LampStateName = "unknown"
    state_since: datetime | None = None


class Settings(_Model):
    protect_host: str = ""
    protect_api_key: str = ""
    protect_verify_ssl: bool = False
    smtp_host: str = ""
    smtp_port: int = Field(587, ge=1, le=65535)
    smtp_security: Literal["starttls", "ssl", "none"] = "starttls"
    smtp_username: str = ""
    smtp_password: str = ""
    email_from: str = ""
    admin_emails: str = ""
    subject_prefix: str = Field("[LampMonitor]", max_length=50)
    public_url: str = ""
    camera_offline_seconds: int = Field(120, ge=10, le=86400)

    # Everything except the SMTP password, which may legitimately contain spaces.
    _strip_text = field_validator(
        "protect_host",
        "protect_api_key",
        "smtp_host",
        "smtp_username",
        "email_from",
        "subject_prefix",
        "public_url",
        mode="before",
    )(_strip)

    @field_validator("admin_emails")
    @classmethod
    def _check_admin_emails(cls, value: str) -> str:
        return _normalize_addresses(value)

    @field_validator("email_from")
    @classmethod
    def _check_email_from(cls, value: str) -> str:
        if value and not _ADDRESS_RE.match(parseaddr(value)[1]):
            raise ValueError("not a valid email address")
        return value

    @field_validator("public_url")
    @classmethod
    def _check_public_url(cls, value: str) -> str:
        if value and not value.lower().startswith(("http://", "https://")):
            raise ValueError("must start with http:// or https://")
        return value.rstrip("/")


def settings_view(settings: Settings) -> dict[str, Any]:
    """Settings as returned by the API: secrets are replaced by whether they are set."""
    data = settings.model_dump(exclude=set(SECRET_FIELDS))
    for name in SECRET_FIELDS:
        data[f"{name}_set"] = bool(getattr(settings, name))
    return data
