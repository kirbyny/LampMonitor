import io
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from lampmonitor import imaging

from .conftest import LAMP_REGION, encode, make_image


def box(**region):
    return SimpleNamespace(**region)


def test_decode_returns_rgb_and_media_type():
    rgb, media_type = imaging.decode(encode(make_image(), "JPEG"))
    assert rgb.shape == (100, 200, 3)
    assert media_type == "image/jpeg"


def test_decode_rejects_non_images():
    with pytest.raises(ValueError, match="not a usable image"):
        imaging.decode(b"<html>login page</html>")


def test_coloured_lamp_reads_as_bright():
    # A pure red lamp has low luminance but full HSV value.
    rgb = make_image([(LAMP_REGION, (255, 0, 0))])
    assert imaging.measure(rgb, box(**LAMP_REGION), "peak") == 255
    assert imaging.measure(rgb, box(**LAMP_REGION), "mean") == 255


def test_peak_ignores_background_in_a_loose_box():
    rgb = make_image([(LAMP_REGION, (250, 250, 250))], background=20)
    loose = box(x=0.05, y=0.1, w=0.2, h=0.4)  # lamp covers about a third of the measured oval
    assert imaging.measure(rgb, loose, "peak") == 250
    assert 60 < imaging.measure(rgb, loose, "mean") < 120


def test_bright_panel_in_box_corners_is_not_measured():
    # An unlit (dark) round lamp on a light grey panel, boxed tightly.
    rgb = np.full((100, 100, 3), 200, dtype=np.uint8)
    yy, xx = np.ogrid[:100, :100]
    rgb[(xx - 49.5) ** 2 + (yy - 49.5) ** 2 <= 20**2] = (40, 40, 40)
    tight = box(x=0.3, y=0.3, w=0.4, h=0.4)
    assert imaging.measure(rgb, tight, "peak") == 40
    assert imaging.measure(rgb, tight, "mean") == 40


def test_pixel_bounds_are_clamped_and_never_empty():
    assert imaging.pixel_bounds(box(x=0.999, y=0.999, w=0.0001, h=0.0001), 200, 100) == (199, 99, 200, 100)
    assert imaging.pixel_bounds(box(x=0, y=0, w=1, h=1), 200, 100) == (0, 0, 200, 100)


def test_annotate_produces_scaled_jpeg():
    rgb = np.zeros((1000, 4000, 3), dtype=np.uint8)
    data = imaging.annotate(rgb, box(**LAMP_REGION), "Circuit 12", max_width=1600)
    image = Image.open(io.BytesIO(data))
    assert image.format == "JPEG"
    assert image.size == (1600, 400)
    # The outline is drawn in red around the lamp.
    r, g, b = image.convert("RGB").getpixel((round(0.1 * 1600) - 3, round(0.3 * 400)))
    assert r > 150 and g < 100 and b < 100
