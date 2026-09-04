from __future__ import annotations

import barcode
from barcode.writer import ImageWriter
from PIL import Image

ESC = b"\x1b"
GS = b"\x1d"

LABEL_WIDTH_DOTS = 384
LABEL_HEIGHT_DOTS = 203
LEFT_MARGIN_DOTS = 6
RIGHT_MARGIN_DOTS = 24
TOP_MARGIN_DOTS = 0
BOTTOM_MARGIN_DOTS = 64

# The T9E-US ignores a feed command after a raster job. Configurable blank
# raster rows advance the finished label far enough to clear the enclosure.

# Kept in the job for compatibility with the printer, although its effective
# darkness is stored in non-volatile printer configuration.
HEATING_DOTS = 9
HEATING_TIME = 80
HEATING_INTERVAL = 2


def render_label(payload: str) -> Image.Image:
    """Render a Code 128 barcode using the layout tuned on the T9E-US."""
    code = barcode.get("code128", payload, writer=ImageWriter())
    barcode_image = code.render(
        writer_options={
            "module_height": 12.0,
            "quiet_zone": 1,
            "write_text": False,
            "font_size": 0,
            "text_distance": 3,
        }
    ).convert("L")

    available_width = LABEL_WIDTH_DOTS - LEFT_MARGIN_DOTS - RIGHT_MARGIN_DOTS
    available_height = LABEL_HEIGHT_DOTS - TOP_MARGIN_DOTS - BOTTOM_MARGIN_DOTS
    source_width, source_height = barcode_image.size
    rendered_height = min(
        available_height,
        round(source_height * (available_width / source_width)),
    )
    barcode_image = barcode_image.resize(
        (available_width, rendered_height),
        Image.Resampling.NEAREST,
    )

    label = Image.new("L", (LABEL_WIDTH_DOTS, LABEL_HEIGHT_DOTS), 255)
    x = LEFT_MARGIN_DOTS + (available_width - barcode_image.width) // 2
    y = TOP_MARGIN_DOTS + (available_height - barcode_image.height) // 2
    label.paste(barcode_image, (x, y))
    return label


def _add_feed_rows(label: Image.Image, feed_dots: int) -> Image.Image:
    if feed_dots < 0:
        raise ValueError("feed_dots must be non-negative")
    raster = Image.new(
        "L",
        (label.width, label.height + feed_dots),
        255,
    )
    raster.paste(label, (0, 0))
    return raster


def _pack_raster(image: Image.Image, *, threshold: int = 128) -> tuple[bytes, int, int]:
    grayscale = image.convert("L")
    width, height = grayscale.size
    bytes_per_row = (width + 7) // 8
    pixels = grayscale.load()
    if pixels is None:
        raise RuntimeError("unable to access label raster pixels")
    packed = bytearray(bytes_per_row * height)

    for y in range(height):
        row_offset = y * bytes_per_row
        for x in range(width):
            pixel = pixels[x, y]
            if not isinstance(pixel, (int, float)):
                raise TypeError("expected a grayscale label raster")
            if pixel < threshold:
                packed[row_offset + x // 8] |= 0x80 >> (x % 8)

    return bytes(packed), bytes_per_row, height


def build_label_job(payload: str, *, feed_dots: int) -> bytes:
    """Build the complete ESC/POS raster job for one label without doing I/O."""
    packed, bytes_per_row, height = _pack_raster(_add_feed_rows(render_label(payload), feed_dots))
    raster_header = (
        GS
        + b"v0"
        + bytes(
            [
                0,
                bytes_per_row & 0xFF,
                bytes_per_row >> 8,
                height & 0xFF,
                height >> 8,
            ]
        )
    )
    density = ESC + b"7" + bytes([HEATING_DOTS, HEATING_TIME, HEATING_INTERVAL])
    return ESC + b"@" + density + raster_header + packed
