#!/usr/bin/env python3
# Print a 1D barcode on a T9E over /dev/usb/lp0
# Usage examples:
#   python3 print_barcode.py "CODE128" "12345678" --height 80 --width 3 --hri below
#   python3 print_barcode.py "CODE39"  "HELLO-123" --height 80 --width 2

import argparse, os, sys

ESC = b"\x1b"
GS  = b"\x1d"

HRI_MAP = {"none":0, "above":1, "below":2, "both":3}

# GS k (mode B) symbology map for this printer
# m values per spec: 65..74 (UPC-A, UPC-E, EAN13, EAN8, CODE39, ITF, CODABAR, CODE93, CODE128, UCC/EAN128)
SYMS = {
    "UPC-A":65, "UPCA":65,
    "UPC-E":66, "UPCE":66,
    "EAN13":67, "JAN13":67,
    "EAN8":68,  "JAN8":68,
    "CODE39":69, "CODE-39":69,
    "ITF":70, "I2OF5":70, "INTERLEAVED2OF5":70,
    "CODABAR":71, "NW-7":71,
    "CODE93":72,
    "CODE128":73,
    "UCC/EAN128":74, "GS1-128":74, "EAN128":74,
}

def die(msg:int):
    print(msg, file=sys.stderr); sys.exit(1)

def set_hri_position(buf:bytearray, where:str):
    n = HRI_MAP.get(where.lower(), 0)
    buf += GS + b"H" + bytes([n])

def set_barcode_height(buf:bytearray, n:int):
    if not (1 <= n <= 255):
        die("height must be 1..255 dots")
    buf += GS + b"h" + bytes([n])

def set_barcode_width(buf:bytearray, n:int):
    if not (1 <= n <= 6):
        die("width must be 1..6 (module dots)")
    buf += GS + b"w" + bytes([n])

def build_barcode_cmd(sym:str, data:bytes):
    m = SYMS.get(sym.upper())
    if m is None:
        die(f"unsupported symbology: {sym}")
    if not (1 <= len(data) <= 255):
        die("data length must be 1..255")
    # Mode B: GS k m n [data]
    return GS + b"k" + bytes([m, len(data)]) + data

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sym", default="CODE128", help="Symbology, e.g. CODE128, CODE39, EAN13, ITF")
    ap.add_argument("--payload", default="beblet", help="Barcode data string")
    ap.add_argument("--device", default="/dev/usb/lp0")
    ap.add_argument("--height", type=int, default=96, help="barcode height in dots (1..255)")
    ap.add_argument("--width",  type=int, default=3,  help="module width in dots (1..6)")
    ap.add_argument("--hri", choices=["none","above","below","both"], default="none")
    ap.add_argument("--raw", action="store_true", help="send payload as-is (no {B} prefix for CODE128)")
    args = ap.parse_args()

    data = args.payload.encode("ascii", "strict")

    # Helpful default for CODE128: auto-select Code Set B unless --raw
    if args.sym.upper() == "CODE128" and not args.raw:
        # Prefix {B to select Code Set B (you can embed {C to switch to numeric)
        data = b"{B" + data

    # Build command stream
    out = bytearray()
    out += ESC + b"@"                 # Initialize
    set_hri_position(out, args.hri)   # GS H n
    set_barcode_height(out, args.height)  # GS h n
    set_barcode_width(out, args.width)    # GS w n
    out += build_barcode_cmd(args.sym, data)
    out += b"\n\n"                    # small feed

    # Write to device
    with open(args.device, "wb", buffering=0) as f:
        f.write(out)

if __name__ == "__main__":
    main()
