#!/usr/bin/env python3
"""
Robust 58mm raster image printer for /dev/usb/lp0 (384 px wide).
Adds darkness control (ESC 7) and optional per-chunk sleep.

Usage:
  python print_raster.py img.png [--dither] [--threshold 128] [--invert]
                                [--feed 80]
                                [--darkness 0..5]
                                [--heat-dots N1 --heat-time N2 --heat-interval N3]
                                [--chunk-sleep 0.0]
"""

import argparse
import time
from PIL import Image

PRINTER_DEV = "/dev/usb/lp0"
PRINT_WIDTH_PX = 384
STRIPE_MAX_ROWS = 256

ESC = b"\x1b"
GS  = b"\x1d"
CAN = b"\x18"

def open_printer(dev=PRINTER_DEV):
    return open(dev, "wb", buffering=0)

# ------------------- Forcing ESC/POS binary mode -------------------

def force_binary_mode(f):
    f.write(CAN * 8)
    f.write(ESC + b"@")                 # Initialize
    f.write(ESC + b"!" + b"\x00")       # Standard mode
    f.write(ESC + b"t" + b"\x00")       # Codepage 0 (CP437)
    f.write(ESC + b"a" + b"\x00")       # Left justify
    f.write(ESC + b"2")                 # Default line spacing
    f.write(ESC + b"3" + b"\x18")       # 24-dot spacing

    # Tiny 1x8 raster probe
    width_bytes = 1
    f.write(GS + b"v" + b"0" + b"\x00" + bytes([width_bytes, 0, 8, 0]))
    f.write(b"\xff" * 8)

# ------------------- Darkness / Heating -------------------

# Handy presets seen on many ESC/POS heads (tune to taste)
# Format: (heating_dots, heating_time, heating_interval)
DARK_PRESETS = {
    0: (6,  60,  2),    # very light / cool
    1: (7,  80,  2),    # default-ish
    2: (9,  100, 3),    # a bit darker
    3: (11, 120, 4),    # dark
    4: (12, 140, 6),    # darker / slower
    5: (14, 160, 8),    # max-ish (watch for power limits)
}

def set_heating(f, n1, n2, n3):
    """
    ESC 7 n1 n2 n3
      n1: heating dots (0-255)
      n2: heating time (0-255)
      n3: heating interval (0-255)
    Higher n1/n2 and lower n3 => darker but slower/hotter.
    """
    f.write(ESC + b"7" + bytes([n1 & 0xFF, n2 & 0xFF, n3 & 0xFF]))

def escpos_feed(f, dots=80):
    if dots <= 0: return
    remain = dots
    while remain > 0:
        step = min(255, remain)
        f.write(ESC + b"J" + bytes([step]))
        remain -= step

# ------------------- Image prep -------------------

def to_bitmap(img: Image.Image, dither: bool, threshold: int, invert: bool) -> Image.Image:
    w, h = img.size
    if w != PRINT_WIDTH_PX:
        new_h = int(round(h * (PRINT_WIDTH_PX / float(w))))
        img = img.resize((PRINT_WIDTH_PX, new_h), Image.LANCZOS)

    if dither:
        img = img.convert("1")
    else:
        img = img.convert("L").point(lambda x: 255 if x >= threshold else 0, mode="1")

    if invert:
        img = Image.eval(img, lambda v: 255 - v)
    return img

def pack_row_bits(row_white255):
    out = bytearray()
    byte = 0
    bit_count = 0
    for p in row_white255:
        bit = 1 if p == 0 else 0  # black -> 1
        byte = (byte << 1) | bit
        bit_count += 1
        if bit_count == 8:
            out.append(byte)
            byte = 0
            bit_count = 0
    if bit_count:
        out.append(byte << (8 - bit_count))
    return bytes(out)

def send_raster_chunk(f, mono: Image.Image, y0: int, y1: int):
    assert mono.mode == "1"
    width = mono.width
    height = y1 - y0
    width_bytes = (width + 7) // 8
    xL, xH = width_bytes & 0xFF, (width_bytes >> 8) & 0xFF
    yL, yH = height & 0xFF, (height >> 8) & 0xFF
    f.write(GS + b"v" + b"0" + b"\x00" + bytes([xL, xH, yL, yH]))
    px = mono.load()
    for y in range(y0, y1):
        row = [px[x, y] for x in range(width)]
        f.write(pack_row_bits(row))

# ------------------- Main entry -------------------

def print_image(path, dither=False, threshold=128, invert=False,
                feed=80, darkness=5, heat_dots=None, heat_time=None,
                heat_interval=None, chunk_sleep=0.0):
    img = Image.open(path)
    mono = to_bitmap(img, dither=dither, threshold=threshold, invert=invert)

    with open_printer() as f:
        force_binary_mode(f)

        # Pick heating parameters
        if heat_dots is not None and heat_time is not None and heat_interval is not None:
            n1, n2, n3 = heat_dots, heat_time, heat_interval
        else:
            level = 1 if darkness is None else max(0, min(5, darkness))
            n1, n2, n3 = DARK_PRESETS[level]
        set_heating(f, n1, n2, n3)

        # Stream in stripes
        h = mono.height
        y = 0
        while y < h:
            y_next = min(y + STRIPE_MAX_ROWS, h)
            send_raster_chunk(f, mono, y, y_next)
            y = y_next
            if chunk_sleep > 0:
                time.sleep(chunk_sleep)

        escpos_feed(f, feed)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("image", help="Path to input image (png/jpg/bmp)")
    ap.add_argument("--dither", action="store_true", help="Use Floyd–Steinberg dithering")
    ap.add_argument("--threshold", type=int, default=128, help="Threshold 0–255 if not using --dither")
    ap.add_argument("--invert", action="store_true", help="Invert black/white if prints come out reversed")
    ap.add_argument("--feed", type=int, default=80, help="Feed dots after print (80≈~10 mm at 203 dpi)")
    ap.add_argument("--darkness", type=int, choices=range(0,6),
                    help="Convenience levels 0..5 (maps to ESC 7 presets)")
    ap.add_argument("--heat-dots", type=int, help="ESC 7 n1: heating dots")
    ap.add_argument("--heat-time", type=int, help="ESC 7 n2: heating time")
    ap.add_argument("--heat-interval", type=int, help="ESC 7 n3: heating interval")
    ap.add_argument("--chunk-sleep", type=float, default=0.0,
                    help="Sleep seconds between raster stripes (stability)")
    args = ap.parse_args()

    print_image(args.image,
                dither=args.dither,
                threshold=args.threshold,
                invert=args.invert,
                feed=args.feed,
                darkness=args.darkness,
                heat_dots=args.heat_dots,
                heat_time=args.heat_time,
                heat_interval=args.heat_interval,
                chunk_sleep=args.chunk_sleep)
