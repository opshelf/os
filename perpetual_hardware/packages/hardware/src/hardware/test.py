#!/usr/bin/env python3
"""
Robust 58mm raster image printer for /dev/usb/lp0 (384 px wide).
- Forces ESC/POS binary mode (flush + reset + sane defaults)
- Resizes to 384 px, converts to 1bpp (threshold or FS dither)
- Streams in stripes to avoid buffer overruns
- Feeds to next label/receipt tail

Usage:
  python print_raster.py path/to/image.png [--dither] [--threshold 128] [--invert] [--feed 80]
"""

import argparse
from PIL import Image

PRINTER_DEV = "/dev/usb/lp0"
PRINT_WIDTH_PX = 384
STRIPE_MAX_ROWS = 256  # conservative stripe height

ESC = b"\x1b"
GS  = b"\x1d"
CAN = b"\x18"          # cancel print buffer

def open_printer(dev=PRINTER_DEV):
    # unbuffered so bytes go out immediately
    return open(dev, "wb", buffering=0)

# ------------------- Forcing ESC/POS binary mode -------------------

def force_binary_mode(f):
    """
    Do everything reasonable to ensure the device interprets bytes as ESC/POS,
    not text:
      - Cancel any pending buffer
      - Reset printer
      - Set codepage to 0 (CP437) so later text is deterministic
      - Left justify, standard mode, known line spacing
      - Small benign raster 'probe' (consumed by most firmwares)
    """
    # 1) Flush/cancel anything the printer was holding (some firmwares buffer text)
    f.write(CAN * 8)  # a few CANs in a row is safe

    # 2) Hard reset to ESC/POS defaults
    f.write(ESC + b"@")                 # Initialize
    f.write(ESC + b"!" + b"\x00")       # Standard mode
    f.write(ESC + b"t" + b"\x00")       # Codepage 0 (CP437)
    f.write(ESC + b"a" + b"\x00")       # Left justify
    f.write(ESC + b"2")                 # Default line spacing
    f.write(ESC + b"3" + b"\x18")       # Line spacing 24 dots (helps LFs if any)

    # 3) Send a tiny 1x8 raster (black vertical line) — harmless but tends to
    #    put devices firmly into graphics/raster parsing paths
    width_bytes = (8 + 7) // 8  # 1 byte wide
    xL, xH = width_bytes & 0xFF, (width_bytes >> 8) & 0xFF
    yL, yH = 8 & 0xFF, (8 >> 8) & 0xFF
    f.write(GS + b"v" + b"0" + b"\x00" + bytes([xL, xH, yL, yH]))
    f.write(b"\xff" * 8)  # 8 rows of 0xFF -> solid 1-pixel-wide black column

def escpos_feed(f, dots=80):
    """
    Feed by dots (ESC J) is better for label printers than 'lines'.
    80 dots ≈ ~10 mm on many 203 dpi heads. Tune as needed.
    """
    if dots <= 0: return
    # ESC J n : print and feed paper by n dots (0–255). For >255, loop.
    remaining = dots
    while remaining > 0:
        step = min(255, remaining)
        f.write(ESC + b"J" + bytes([step]))
        remaining -= step

# ------------------- Image prep -------------------

def to_bitmap(img: Image.Image, dither: bool, threshold: int, invert: bool) -> Image.Image:
    # Resize to width = 384 px, keep aspect ratio
    w, h = img.size
    if w != PRINT_WIDTH_PX:
        new_h = int(round(h * (PRINT_WIDTH_PX / float(w))))
        img = img.resize((PRINT_WIDTH_PX, new_h), Image.LANCZOS)

    # Convert to monochrome
    if dither:
        img = img.convert("1")  # FS dither
    else:
        img = img.convert("L").point(lambda x: 255 if x >= threshold else 0, mode="1")

    if invert:
        img = Image.eval(img, lambda v: 255 - v)  # invert 1bpp
    return img

def pack_row_bits(row_white255):
    """
    Row is a sequence of 0/255 (from '1' mode). We want black=1 bit.
    """
    out = bytearray()
    byte = 0
    bit_count = 0
    for p in row_white255:
        bit = 1 if p == 0 else 0  # black pixel -> 1
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
    """
    GS v 0 m xL xH yL yH [data]
    m=0 (normal)
    """
    assert mono.mode == "1"
    width = mono.width
    height = y1 - y0
    width_bytes = (width + 7) // 8
    xL = width_bytes & 0xFF
    xH = (width_bytes >> 8) & 0xFF
    yL = height & 0xFF
    yH = (height >> 8) & 0xFF

    f.write(GS + b"v" + b"0" + b"\x00" + bytes([xL, xH, yL, yH]))

    # PIL '1' gives 0/255; 0 == black
    pix = mono.load()
    for y in range(y0, y1):
        row = [pix[x, y] for x in range(width)]
        f.write(pack_row_bits(row))

# ------------------- Main entry -------------------

def print_image(path, dither=False, threshold=128, invert=False, feed=80):
    img = Image.open(path)
    mono = to_bitmap(img, dither=dither, threshold=threshold, invert=invert)

    with open_printer() as f:
        force_binary_mode(f)  # <<< the important bit
        # Stream in stripes
        h = mono.height
        y = 0
        while y < h:
            y_next = min(y + STRIPE_MAX_ROWS, h)
            send_raster_chunk(f, mono, y, y_next)
            y = y_next
        # Feed to separate labels or give tail on receipt
        escpos_feed(f, feed)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("image", help="Path to input image (png/jpg/bmp)")
    ap.add_argument("--dither", action="store_true", help="Use Floyd–Steinberg dithering")
    ap.add_argument("--threshold", type=int, default=128, help="Threshold 0–255 if not using --dither")
    ap.add_argument("--invert", action="store_true", help="Invert black/white if prints come out reversed")
    ap.add_argument("--feed", type=int, default=80, help="Feed dots after print (80≈~10mm on 203dpi)")
    args = ap.parse_args()
    print_image(args.image, dither=args.dither, threshold=args.threshold, invert=args.invert, feed=args.feed)
