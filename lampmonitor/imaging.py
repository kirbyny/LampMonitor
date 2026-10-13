"""Image decoding, lamp brightness measurement and alert snapshot annotation."""

from __future__ import annotations

import functools
import io
from typing import Protocol

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# "peak" brightness is this percentile of the region, so the box can be drawn
# loosely around a lamp without the dark surroundings diluting the reading.
PEAK_PERCENTILE = 90

# Fonts with good Unicode coverage (Debian's fonts-dejavu-core, macOS, Windows). Pillow's
# built-in fallback font lacks typographic punctuation, so labels are folded to ASCII for it.
_LABEL_FONTS = ("DejaVuSans.ttf", "Arial.ttf", "arial.ttf")
_TO_ASCII = str.maketrans(
    {"\u2013": "-", "\u2014": "-", "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2026": "..."}
)


class Box(Protocol):
    x: float
    y: float
    w: float
    h: float


def decode(data: bytes) -> tuple[np.ndarray, str]:
    """Decode an encoded image into an RGB array (height x width x 3) and its MIME type."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            media_type = Image.MIME.get(image.format or "", "application/octet-stream")
            return np.asarray(image.convert("RGB")), media_type
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ValueError(f"camera returned data that is not a usable image ({len(data)} bytes)") from exc


def pixel_bounds(box: Box, width: int, height: int) -> tuple[int, int, int, int]:
    """Convert a normalized box into pixel bounds (x0, y0, x1, y1), at least one pixel in size."""
    x0 = min(max(round(box.x * width), 0), width - 1)
    y0 = min(max(round(box.y * height), 0), height - 1)
    x1 = min(max(round((box.x + box.w) * width), x0 + 1), width)
    y1 = min(max(round((box.y + box.h) * height), y0 + 1), height)
    return x0, y0, x1, y1


def region_values(rgb: np.ndarray, box: Box) -> np.ndarray:
    """HSV "value" (the brightest of R, G and B) of the pixels inside the oval inscribed in ``box``.

    Using the value channel means a lit red or green lamp reads as bright as a
    white one. Panel lamps are round, so measuring the inscribed oval keeps the
    panel paint in the corners of the box (often brighter than an unlit lamp)
    out of the reading.
    """
    height, width = rgb.shape[:2]
    x0, y0, x1, y1 = pixel_bounds(box, width, height)
    value = rgb[y0:y1, x0:x1].max(axis=2)
    h, w = value.shape
    if h < 3 or w < 3:
        return value.ravel()
    yy, xx = np.ogrid[:h, :w]
    inside = ((xx + 0.5 - w / 2) / (w / 2)) ** 2 + ((yy + 0.5 - h / 2) / (h / 2)) ** 2 <= 1
    return value[inside]


def measure(rgb: np.ndarray, box: Box, metric: str = "peak") -> float:
    """Brightness (0-255) of a lamp region: its brightest spot ("peak") or its average ("mean")."""
    value = region_values(rgb, box)
    if metric == "mean":
        return float(value.mean())
    return float(np.percentile(value, PEAK_PERCENTILE))


def annotate(
    rgb: np.ndarray,
    box: Box,
    label: str,
    *,
    color: tuple[int, int, int] = (220, 38, 38),
    max_width: int = 1600,
) -> bytes:
    """Return a JPEG of the image with ``box`` outlined and labelled."""
    image = Image.fromarray(rgb)
    if image.width > max_width:
        image = image.resize(
            (max_width, max(1, round(image.height * max_width / image.width))), Image.Resampling.LANCZOS
        )
    draw = ImageDraw.Draw(image)
    x0, y0, x1, y1 = pixel_bounds(box, image.width, image.height)
    line = max(2, round(min(image.size) / 180))
    pad = line + 2
    draw.rectangle((x0 - pad, y0 - pad, x1 + pad - 1, y1 + pad - 1), outline=color, width=line)

    font, unicode_ok = _label_font(max(12, round(image.height / 28)))
    if not unicode_ok:
        label = label.translate(_TO_ASCII)
    left, top, right, bottom = draw.textbbox((0, 0), label, font=font)
    text_w, text_h = right - left, bottom - top
    tx = min(max(x0 - pad, 0), max(image.width - text_w - 8, 0))
    ty = y0 - pad - text_h - 8
    if ty < 0:
        ty = min(y1 + pad, image.height - text_h - 8)
    draw.rectangle((tx, ty, tx + text_w + 8, ty + text_h + 8), fill=color)
    draw.text((tx + 4 - left, ty + 4 - top), label, fill=(255, 255, 255), font=font)

    out = io.BytesIO()
    image.save(out, format="JPEG", quality=85)
    return out.getvalue()


@functools.lru_cache(maxsize=8)
def _label_font(size: int) -> tuple[ImageFont.ImageFont | ImageFont.FreeTypeFont, bool]:
    for name in _LABEL_FONTS:
        try:
            return ImageFont.truetype(name, size), True
        except OSError:
            continue
    return ImageFont.load_default(size=size), False
