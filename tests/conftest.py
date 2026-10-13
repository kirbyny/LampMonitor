from __future__ import annotations

import io
from collections.abc import Iterable

import numpy as np
from PIL import Image

from lampmonitor.schemas import Settings

WIDTH, HEIGHT = 200, 100

# The lamp used throughout the tests occupies this normalized region.
LAMP_REGION = {"x": 0.1, "y": 0.2, "w": 0.1, "h": 0.2}


def make_image(
    lamps: Iterable[tuple[dict, tuple[int, int, int]]] = (), *, background: int = 30
) -> np.ndarray:
    """An RGB test image; each lamp is (normalized region, colour) and is painted as a solid box."""
    rgb = np.full((HEIGHT, WIDTH, 3), background, dtype=np.uint8)
    for region, color in lamps:
        x0, y0 = round(region["x"] * WIDTH), round(region["y"] * HEIGHT)
        x1, y1 = round((region["x"] + region["w"]) * WIDTH), round((region["y"] + region["h"]) * HEIGHT)
        rgb[y0:y1, x0:x1] = color
    return rgb


def panel(lit: bool) -> np.ndarray:
    return make_image([(LAMP_REGION, (40, 230, 60) if lit else (35, 45, 38))])


def encode(rgb: np.ndarray, fmt: str = "PNG") -> bytes:
    out = io.BytesIO()
    Image.fromarray(rgb).save(out, format=fmt)
    return out.getvalue()


def mail_settings(**overrides) -> Settings:
    values = {
        "smtp_host": "smtp.example.com",
        "email_from": "lampmonitor@example.com",
        "admin_emails": "admin@example.com",
        **overrides,
    }
    return Settings(**values)


class FakeSource:
    """A frame source whose next result can be changed between fetches."""

    def __init__(self, data: bytes | None = None) -> None:
        self.data = data
        self.error: Exception | None = None
        self.fetches = 0

    async def fetch(self) -> bytes:
        self.fetches += 1
        if self.error is not None:
            raise self.error
        return self.data

    async def close(self) -> None:
        pass


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.error: Exception | None = None

    async def send(self, settings, recipients, subject, body, image=None) -> None:
        if self.error is not None:
            raise self.error
        self.sent.append({"recipients": recipients, "subject": subject, "body": body, "image": image})


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now
