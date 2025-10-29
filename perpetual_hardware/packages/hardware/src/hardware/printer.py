#!/usr/bin/env python3
from __future__ import annotations
import time
import secrets
from typing import Optional, Tuple

from PIL import Image
from escpos.printer import Usb, File, Network  # type: ignore

# Optional image-based generation
try:
    import barcode as bc
    from barcode.writer import ImageWriter
except Exception:
    bc = None
    ImageWriter = None


class BarcodePrinter:
    """
    ESC/POS Barcode Printer wrapper supporting:
      - Backends: USB, /dev/usb/lp0 (File), Network
      - Modes: image (python-barcode -> raster) or native ESC/POS
      - A helper to print a RANDOM valid barcode for common symbologies
    """

    # ---------- Construction / Connection ----------
    def __init__(
        self,
        *,
        usb: Optional[str] = None,      # "0xVID:0xPID"
        in_ep: Optional[str] = None,    # "0x82"
        out_ep: Optional[str] = None,   # "0x01"
        usb_timeout_ms: int = 2000,
        device: Optional[str] = None,   # "/dev/usb/lp0"
        host: Optional[str] = None,     # "192.168.1.50"
        port: int = 9100,
        default_align: str = "center",
    ) -> None:
        self._backend_args = dict(
            usb=usb, in_ep=in_ep, out_ep=out_ep, usb_timeout_ms=usb_timeout_ms,
            device=device, host=host, port=port,
        )
        self._p = None
        self._default_align = default_align

    @staticmethod
    def _parse_vid_pid(arg: str) -> Tuple[int, int]:
        if ":" not in arg:
            raise ValueError("VID:PID must be '0xVID:0xPID' (e.g. 0x0416:0x5011)")
        v, p = arg.split(":", 1)
        return int(v, 16), int(p, 16)

    def _connect(self):
        if self._p is not None:
            return self._p
        args = self._backend_args
        if args["usb"]:
            vid, pid = self._parse_vid_pid(args["usb"])
            in_ep = int(args["in_ep"], 16) if args["in_ep"] else None
            out_ep = int(args["out_ep"], 16) if args["out_ep"] else None
            self._p = Usb(vid, pid, in_ep=in_ep, out_ep=out_ep, timeout=args["usb_timeout_ms"])
        elif args["device"]:
            self._p = File(args["device"])
        elif args["host"]:
            self._p = Network(args["host"], port=args["port"])
        else:
            raise RuntimeError("Specify one of usb='0xVID:0xPID', device='/dev/usb/lp0', or host='IP'.")
        # light defaults
        try:
            self._p.set(align=self._default_align)
        except Exception:
            pass
        time.sleep(0.05)
        return self._p

    def close(self) -> None:
        if self._p:
            try:
                self._p.close()
            finally:
                self._p = None

    # ---------- Cut / feed ----------
    def _cut_safe(self, mode: str = "full") -> None:
        p = self._connect()
        try:
            if mode == "partial":
                p.cut("PART")
            else:
                p.cut("FULL")
            return
        except Exception:
            pass
        try:
            p.text("\n\n\n\n")
        except Exception:
            pass

    # ---------- Image generation (image mode) ----------
    @staticmethod
    def _generate_barcode_image(symbology: str, data: str, width_px: int, height_px: int,
                                text: Optional[str], quiet_zone: int) -> Image.Image:
        if bc is None or ImageWriter is None:
            raise RuntimeError("python-barcode[images] is required for image mode. pip install python-barcode[images]")
        name_map = {
            "code128": "code128", "ean13": "ean13", "ean8": "ean8", "upca": "upca",
            "isbn13": "isbn13", "issn": "issn", "code39": "code39", "itf": "itf", "jan": "jan",
        }
        key = name_map.get(symbology.lower())
        if key is None:
            raise ValueError(f"Unsupported symbology for image mode: {symbology}")
        writer = ImageWriter()
        options = {
            "write_text": bool(text),
            "text": text or data,
            "quiet_zone": quiet_zone,
            "font_size": 12,
            "module_width": 0.2,
            "module_height": 30.0,
            "foreground": "black",
            "background": "white",
        }
        BarcodeClass = bc.get_barcode_class(key)
        b = BarcodeClass(data, writer=writer)
        pil_img = b.render(writer_options=options).convert("L")
        pil_img = pil_img.resize((width_px, height_px), Image.LANCZOS).convert("1")
        return pil_img

    def _print_image(self, img: Image.Image, align: str = "center", high_density: bool = True) -> None:
        p = self._connect()
        try:
            p.set(align=align)
        except Exception:
            pass
        p.image(img, high_density_vertical=high_density, high_density_horizontal=high_density, impl="bitImageRaster")

    # ---------- Native barcode ----------
    def _print_native_barcode(self, symbology: str, data: str, height: int, width: int,
                              hri: str = "below", align: str = "center") -> None:
        p = self._connect()
        try:
            p.set(align=align)
        except Exception:
            pass
        hri_map = {"none": 0, "above": 1, "below": 2, "both": 3}
        p._raw(b"\x1D\x48" + bytes([hri_map.get(hri, 2)]))              # GS H
        if 2 <= width <= 6:
            p._raw(b"\x1D\x77" + bytes([width]))                        # GS w
        p._raw(b"\x1D\x68" + bytes([max(1, min(255, height))]))         # GS h
        type_map = {
            "code128": "CODE128", "ean13": "EAN13", "ean8": "EAN8",
            "upca": "UPC-A", "code39": "CODE39", "itf": "ITF",
        }
        escpos_type = type_map.get(symbology.lower())
        if escpos_type is None:
            raise ValueError(f"Unsupported symbology for native mode: {symbology}")
        p.barcode(data, escpos_type, width=width, height=height, pos=hri.upper() if hri != "none" else "OFF")

    # ---------- Public: print a specific barcode ----------
    # Add this helper to your class
    def _drain(self, seconds: float = 0.25) -> None:
        # With python-escpos we can't fsync the kernel directly; a small sleep is practical and reliable.
        if seconds > 0:
            time.sleep(seconds)

    # Hardened image printing path
    def _print_image(self, img: Image.Image, align: str = "center", high_density: bool = True,
                    fragment_height: int = 256) -> None:
        p = self._connect()
        try:
            p.set(align=align)
        except Exception:
            pass

        # Try bitImageRaster first, then graphics as a fallback
        try:
            p.image(
                img,
                high_density_vertical=high_density,
                high_density_horizontal=high_density,
                impl="bitImageRaster",
                fragment_height=fragment_height,   # <- split tall images to avoid truncation
            )
        except Exception:
            # Some printers prefer 'graphics'
            p.image(
                img,
                high_density_vertical=high_density,
                high_density_horizontal=high_density,
                impl="graphics",
                fragment_height=fragment_height,
            )

    def print_barcode(
        self,
        *,
        data: str,
        symbology: str = "code128",
        mode: str = "image",
        align: str = "center",
        # image-mode sizing:
        img_width: int = 384,
        img_height: int = 160,
        quiet: int = 6,
        label: Optional[str] = None,
        # native-mode sizing:
        height: int = 80,
        width: int = 3,
        hri: str = "below",
        feed: int = 3,
        cut: str = "full",
        # NEW: tweakables
        drain_after_image: float = 0.25,
        pre_cut_feed: int = 2,
        fragment_height: int = 256,
    ) -> None:
        p = self._connect()
        try:
            if mode == "image":
                img = self._generate_barcode_image(symbology, data, img_width, img_height, label, quiet)
                self._print_image(img, align=align, high_density=True, fragment_height=fragment_height)

                # ---- IMPLEMENTATION OF THE ADVICE ----
                # 1) small drain after printing the image
                self._drain(drain_after_image)
                # 2) feed a few lines BEFORE cutting
                if pre_cut_feed > 0:
                    p.text("\n" * int(pre_cut_feed))

            else:
                self._print_native_barcode(symbology, data, height, width, hri=hri, align=align)

            # Keep your usual end-of-job feed & cut
            if feed > 0:
                p.text("\n" * int(feed))
            if cut != "none":
                self._cut_safe("partial" if cut == "partial" else "full")

            # Optional tiny drain at the very end
            self._drain(0.2)

        finally:
            pass  # keep connection open unless you prefer to close each job

    # ---------- Random data generators with valid checksums ----------
    @staticmethod
    def _rand_digits(n: int) -> str:
        return "".join(secrets.choice("0123456789") for _ in range(n))

    @staticmethod
    def _ean13_chk(s12: str) -> str:
        assert len(s12) == 12 and s12.isdigit()
        # sum(odd)*1 + sum(even)*3 (positions 1..12), check = (10 - (sum % 10)) % 10
        total = sum(int(d) for i, d in enumerate(s12, 1) if i % 2 == 1) \
              + 3 * sum(int(d) for i, d in enumerate(s12, 1) if i % 2 == 0)
        return str((10 - (total % 10)) % 10)

    @staticmethod
    def _ean8_chk(s7: str) -> str:
        assert len(s7) == 7 and s7.isdigit()
        # weights 3,1,3,1,3,1,3 on digits 1..7
        weights = [3,1,3,1,3,1,3]
        total = sum(int(d)*w for d, w in zip(s7, weights))
        return str((10 - (total % 10)) % 10)

    @staticmethod
    def _upca_chk(s11: str) -> str:
        # UPC-A checksum is EAN-13 over 12 digits starting with digit 1..11 + check
        # same formula as EAN-13 with 12 digits:
        return BarcodePrinter._ean13_chk(s11 + "0")  # trick: compute with placeholder then ignore; or recalc directly

    def random_payload(self, symbology: str) -> str:
        s = symbology.lower()
        if s == "ean13":
            base = self._rand_digits(12)
            return base + self._ean13_chk(base)
        if s == "ean8":
            base = self._rand_digits(7)
            return base + self._ean8_chk(base)
        if s == "upca":
            base = self._rand_digits(11)
            # UPC-A check: odd-even weighting on 11 digits
            odd = sum(int(d) for i, d in enumerate(base, 1) if i % 2 == 1)
            even = sum(int(d) for i, d in enumerate(base, 1) if i % 2 == 0)
            chk = (10 - ((odd * 3 + even) % 10)) % 10
            return base + str(chk)
        if s in ("code39", "itf"):
            # digits & uppercase for Code39; digits only for ITF
            alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ" if s == "code39" else "0123456789"
            length = 10 if s == "code39" else 12
            return "".join(secrets.choice(alphabet) for _ in range(length))
        # default Code128: use digits+upper for simplicity (CODE128 can encode more)
        return "".join(secrets.choice("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(12))

    # ---------- Public: print a RANDOM barcode ----------
    def print_random_barcode(
        self,
        *,
        symbology: str = "code128",
        mode: str = "image",
        label: Optional[str] = None,
        **kwargs
    ) -> str:
        """
        Generate a valid random payload for the given symbology and print it.
        Returns the payload string.
        Extra keyword args are passed to print_barcode (feed, cut, sizing, etc.).
        """
        payload = self.random_payload(symbology)
        self.print_barcode(
            data=payload,
            symbology=symbology,
            mode=mode,
            label=(label if label is not None else payload) if mode == "image" else None,
            **kwargs,
        )
        return payload


# ---------------------- Example usage ----------------------
if __name__ == "__main__":
    # Example: /dev/usb/lp0, print a random Code128 as an image at 384 px width.
    bp = BarcodePrinter(device="/dev/usb/lp0")
    try:
        payload = bp.print_random_barcode(
            symbology="code128",
            mode="image",
            img_width=384,
            img_height=140,
            feed=4,
            cut="full",
        )
        print("Printed random payload:", payload)
    finally:
        bp.close()
