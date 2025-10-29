#!/usr/bin/env python3
"""
Print a barcode to an ESC/POS printer using python-escpos.

Two modes:
  1) image  (default): Generate a barcode image (Code128, EAN13, etc.) and print it as a raster image.
  2) native: Use the printer's ESC/POS BARCODE command directly.

Connection backends:
  - USB:     --usb 0xVID:0xPID [--in_ep 0x82 --out_ep 0x01]
  - File:    --device /dev/usb/lp0
  - Network: --host 192.168.1.50 [--port 9100]

Requires:
  pip install python-escpos pillow python-barcode[images]
"""

from __future__ import annotations
import argparse
import sys
import time
from typing import Optional, Tuple

from PIL import Image
from escpos.printer import Usb, File, Network  # type: ignore

# Image-based barcode generation
try:
    import barcode as bc  # python-barcode
    from barcode.writer import ImageWriter
except Exception:
    bc = None
    ImageWriter = None


# ---------------------- Helpers ----------------------

def parse_vid_pid(arg: str) -> Tuple[int, int]:
    """
    Parse '0x0416:0x5011' or '0416:5011' into (VID, PID) ints.
    """
    if ":" not in arg:
        raise argparse.ArgumentTypeError("Format must be 0xVID:0xPID (e.g., 0x0416:0x5011)")
    vid_s, pid_s = arg.split(":", 1)
    vid = int(vid_s, 16)
    pid = int(pid_s, 16)
    return vid, pid


def build_printer(args):
    """
    Construct an escpos printer instance from CLI args.
    """
    if args.usb:
        vid, pid = parse_vid_pid(args.usb)
        in_ep = int(args.in_ep, 16) if args.in_ep else None
        out_ep = int(args.out_ep, 16) if args.out_ep else None
        return Usb(vid, pid, in_ep=in_ep, out_ep=out_ep, timeout=args.usb_timeout_ms)
    if args.device:
        return File(args.device)
    if args.host:
        return Network(args.host, port=args.port)
    raise SystemExit("You must specify one of --usb, --device, or --host.")


def set_defaults(p, speed: int = 2, density: int = 10):
    """
    Apply some sensible defaults. Not all printers honor these; safe to ignore errors.
    """
    # python-escpos .set controls font/size/bold/align; feed small init
    try:
        p.set(align="center")
    except Exception:
        pass
    # Some printers expose HW-specific density/speed via GS ( E ...;
    # python-escpos generally doesn’t wrap that. We'll just add tiny delays.
    time.sleep(0.05)


def cut_safe(p, mode: str = "full"):
    """
    Try multiple cut variants; ignore if not supported.
    """
    try:
        if mode == "partial":
            p.cut("PART")
        else:
            p.cut("FULL")
        return
    except Exception:
        pass
    # Fallback: feed lines if no cutter
    try:
        p.text("\n\n\n\n")
    except Exception:
        pass


def generate_barcode_image(symbology: str, data: str, width_px: int, height_px: int,
                           text: Optional[str], quiet_zone: int) -> Image.Image:
    """
    Generate a barcode image using python-barcode + Pillow and return a PIL Image.
    """
    if bc is None or ImageWriter is None:
        raise RuntimeError("python-barcode[images] is required for image mode. pip install python-barcode[images]")

    # Map user symbology to python-barcode names
    name_map = {
        "code128": "code128",
        "ean13": "ean13",
        "ean8": "ean8",
        "upca": "upca",
        "isbn13": "isbn13",
        "issn": "issn",
        "code39": "code39",
        "itf": "itf",
        "jan": "jan",
    }
    key = name_map.get(symbology.lower())
    if key is None:
        raise ValueError(f"Unsupported symbology for image mode: {symbology}")

    writer = ImageWriter()
    # writer options are in modules per python-barcode
    options = {
        "write_text": bool(text),        # draw text under bars
        "text": text or data,            # explicit label if requested
        "quiet_zone": quiet_zone,        # side margins in 'module' units
        "font_size": 12,
        "module_width": 0.2,             # mm per module (affects final width)
        "module_height": 30.0,           # mm height of bars (we'll resize anyway)
        "foreground": "black",
        "background": "white",
    }

    BarcodeClass = bc.get_barcode_class(key)
    b = BarcodeClass(data, writer=writer)
    pil_img = b.render(writer_options=options).convert("L")  # grayscale

    # Resize to target width/height for your 58mm printer (commonly ~384 px wide)
    pil_img = pil_img.resize((width_px, height_px), Image.LANCZOS).convert("1")
    return pil_img


def print_image_with_escpos(p, img: Image.Image, align: str = "center", high_density: bool = True):
    """
    Print a PIL Image using python-escpos' raster image path (bitImageRaster).
    """
    try:
        p.set(align=align)
    except Exception:
        pass

    # python-escpos accepts a PIL Image directly
    # impl options: 'bitImageRaster' (most reliable), 'graphics' (newer), 'bitImageColumn'
    p.image(img, high_density_vertical=high_density, high_density_horizontal=high_density, impl="bitImageRaster")


def print_native_barcode(p, symbology: str, data: str, height: int, width: int,
                         hri: str = "below", align: str = "center"):
    """
    Use ESC/POS native barcode command. width is 2..6 (module width), height in dots.
    hri: 'none'|'above'|'below'|'both'
    """
    try:
        p.set(align=align)
    except Exception:
        pass

    # Set HRI position
    hri_map = {
        "none": 0, "above": 1, "below": 2, "both": 3
    }
    p._raw(b"\x1D\x48" + bytes([hri_map.get(hri, 2)]))  # GS H

    # Set module width and height if supported
    if 2 <= width <= 6:
        p._raw(b"\x1D\x77" + bytes([width]))            # GS w
    p._raw(b"\x1D\x68" + bytes([max(1, min(255, height))]))  # GS h

    # Map to python-escpos barcode types
    # (common: 'CODE128', 'EAN13', 'EAN8', 'UPC-A', 'CODE39', 'ITF')
    type_map = {
        "code128": "CODE128",
        "ean13": "EAN13",
        "ean8": "EAN8",
        "upca": "UPC-A",
        "code39": "CODE39",
        "itf": "ITF",
    }
    escpos_type = type_map.get(symbology.lower())
    if escpos_type is None:
        raise ValueError(f"Unsupported symbology for native mode: {symbology}")

    p.barcode(data, escpos_type, width=width, height=height, pos=hri.upper() if hri != "none" else "OFF")


# ---------------------- CLI ----------------------

def main():
    ap = argparse.ArgumentParser(description="Print a barcode to an ESC/POS printer.")
    conn = ap.add_argument_group("Connection")
    conn.add_argument("--usb", help="USB device as 0xVID:0xPID (e.g. 0x0416:0x5011)")
    conn.add_argument("--in-ep", default=None, help="USB IN endpoint (e.g. 0x82)")
    conn.add_argument("--out-ep", default=None, help="USB OUT endpoint (e.g. 0x01)")
    conn.add_argument("--usb-timeout-ms", type=int, default=2000)
    conn.add_argument("--device", help="File device (e.g. /dev/usb/lp0)")
    conn.add_argument("--host", help="Printer IP (for network printers)")
    conn.add_argument("--port", type=int, default=9100)

    job = ap.add_argument_group("Job")
    job.add_argument("--mode", choices=["image", "native"], default="image", help="Print mode")
    job.add_argument("--symbology", default="code128",
                     help="Barcode symbology (image: code128, ean13, ean8, upca, code39, itf, ...; native: limited set)")
    job.add_argument("--data", required=True, help="Data to encode in the barcode")
    job.add_argument("--label", default=None, help="Optional text under the barcode (image mode)")
    job.add_argument("--align", choices=["left", "center", "right"], default="center")

    # Image-mode sizing
    job.add_argument("--img-width", type=int, default=384, help="Target image width in px (58mm printer ~384)")
    job.add_argument("--img-height", type=int, default=160, help="Target image height in px")
    job.add_argument("--quiet", type=int, default=6, help="Quiet zone (modules) for image barcodes")

    # Native-mode sizing
    job.add_argument("--height", type=int, default=80, help="Native barcode height (dots)")
    job.add_argument("--width", type=int, default=3, help="Native module width (2..6)")
    job.add_argument("--hri", choices=["none", "above", "below", "both"], default="below", help="Human-readable text position")

    job.add_argument("--feed", type=int, default=3, help="Lines to feed after printing")
    job.add_argument("--cut", choices=["none", "full", "partial"], default="full")

    args = ap.parse_args()

    p = build_printer(args)
    try:
        set_defaults(p)

        if args.mode == "image":
            img = generate_barcode_image(
                symbology=args.symbology,
                data=args.data,
                width_px=args.img_width,
                height_px=args.img_height,
                text=args.label,
                quiet_zone=args.quiet,
            )
            print_image_with_escpos(p, img, align=args.align, high_density=True)

        else:  # native
            print_native_barcode(
                p,
                symbology=args.symbology,
                data=args.data,
                height=args.height,
                width=args.width,
                hri=args.hri,
                align=args.align,
            )

        # Feed & cut
        if args.feed > 0:
            p.text("\n" * args.feed)
        if args.cut != "none":
            cut_safe(p, mode=("partial" if args.cut == "partial" else "full"))

        # Small drain delay for some printers
        time.sleep(0.2)

    finally:
        try:
            p.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
