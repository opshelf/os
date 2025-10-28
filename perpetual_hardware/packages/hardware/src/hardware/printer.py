#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple, Union
import os, time
from PIL import Image

PathLike = Union[str, Path]

import asyncio
from contextlib import suppress

class AsyncPrinter:
    def __init__(self, esc_printer):
        self._p = esc_printer          # instance of ESCImagePrinter
        self._lock = asyncio.Lock()    # serialize device access

    async def print_image(self, path: str, **kwargs) -> None:
        """
        Run the blocking print in a worker thread, with:
        - a global lock to prevent interleaving
        - cancellation shielding so partial jobs still finish a band/feed
        """
        async with self._lock:
            # Keep the print from being torn down mid-write
            async def _run():
                # run sync code in a thread; propagate exceptions
                return await asyncio.to_thread(self._p.print_image, path, **kwargs)

            # Shield from cancellation; if cancelled, try to let current band finish
            with suppress(asyncio.CancelledError):
                await asyncio.shield(_run())


class ESCImagePrinter:
    ESC = b"\x1b"; GS = b"\x1d"; CAN = b"\x18"

    DARK_PRESETS = {
        0: (6,  60,  2),
        1: (7,  80,  2),
        2: (9,  100, 3),
        3: (11, 120, 4),
        4: (12, 140, 6),
        5: (14, 160, 8),
    }

    def __init__(self,
                 device: str = "/dev/usb/lp0",
                 print_width_px: int = 384,
                 default_darkness: int = 3,
                 bytes_per_write: int = 1024,
                 band_height: int = 24,
                 band_feed_dots: Optional[int] = 24,  # feed per band; 24 dots ~ one 24-dot band
                 chunk_sleep: float = 0.0,
                 fsync_each_band: bool = True,
                 send_crlf_after_band: bool = True) -> None:
        self.device = device
        self.print_width_px = int(print_width_px)
        self.default_darkness = int(default_darkness)
        self.bytes_per_write = int(bytes_per_write)
        self.band_height = int(band_height)        # 24 for m=33
        self.band_feed_dots = band_feed_dots
        self.chunk_sleep = float(chunk_sleep)
        self.fsync_each_band = bool(fsync_each_band)
        self.send_crlf_after_band = bool(send_crlf_after_band)

    # ---------------- Public API ----------------
    def print_image(self,
                    image: PathLike | Image.Image,
                    *,
                    dither: bool = False,
                    threshold: int = 128,
                    invert: bool = False,
                    feed_dots: int = 80,
                    darkness: Optional[int] = None,
                    heat_params: Optional[Tuple[int,int,int]] = None) -> None:
        mono = self._prepare_bitmap(image, dither=dither, threshold=threshold, invert=invert)
        with self._open_printer() as f:
            self._force_binary_mode(f)
            # heating/darkness
            if heat_params is not None:
                n1, n2, n3 = heat_params
            else:
                level = self.default_darkness if darkness is None else max(0, min(5, darkness))
                n1, n2, n3 = self.DARK_PRESETS[level]
            self._set_heating(f, n1, n2, n3)

            self._print_bands_bitimage(f, mono)

            self._feed_dots(f, feed_dots)

    # ---------------- Core I/O ----------------
    def _open_printer(self):
        return open(self.device, "wb", buffering=0)

    def _force_binary_mode(self, f) -> None:
        f.write(self.CAN * 8)
        f.write(self.ESC + b"@")                 # Initialize
        f.write(self.ESC + b"!" + b"\x00")       # Standard mode
        f.write(self.ESC + b"t" + b"\x00")       # CP437
        f.write(self.ESC + b"a" + b"\x00")       # Left
        f.write(self.ESC + b"2")                 # Default line spacing
        f.write(self.ESC + b"3" + b"\x18")       # 24-dot spacing

    def _set_heating(self, f, n1: int, n2: int, n3: int) -> None:
        f.write(self.ESC + b"7" + bytes([(n1 & 0xFF), (n2 & 0xFF), (n3 & 0xFF)]))

    def _feed_dots(self, f, dots: int) -> None:
        if dots <= 0: return
        remaining = dots
        while remaining > 0:
            step = min(255, remaining)
            f.write(self.ESC + b"J" + bytes([step]))
            remaining -= step

    # ---------------- Image pipeline ----------------
    def _prepare_bitmap(self,
                        image: PathLike | Image.Image,
                        *,
                        dither: bool, threshold: int, invert: bool) -> Image.Image:
        img = image if isinstance(image, Image.Image) else Image.open(str(image))
        w, h = img.size
        if w != self.print_width_px:
            new_h = int(round(h * (self.print_width_px / float(w))))
            img = img.resize((self.print_width_px, new_h), Image.LANCZOS)
        if dither:
            img = img.convert("1")
        else:
            img = img.convert("L").point(lambda x: 255 if x >= threshold else 0, mode="1")
        if invert:
            img = Image.eval(img, lambda v: 255 - v)
        return img.convert("1")

    # ---------------- ESC * printing (24-dot bands) ----------------
    def _print_bands_bitimage(self, f, mono: Image.Image) -> None:
        width_px, height_px = mono.size
        n = width_px                   # n is columns in dots
        nL, nH = (n & 0xFF), ((n >> 8) & 0xFF)
        px = mono.load()

        for y0 in range(0, height_px, self.band_height):
            # 1) Header: ESC * 33 nL nH
            header = self.ESC + b"*" + bytes([33, nL, nH])
            f.write(header)

            # 2) Build band bytes: for each x column -> 3 bytes (top/mid/bot 8 rows)
            band = bytearray()
            for x in range(width_px):
                # top 8
                b0 = 0
                for bit in range(8):
                    yy = y0 + bit
                    if yy < height_px and px[x, yy] == 0:
                        b0 |= (1 << (7 - bit))
                # mid 8
                b1 = 0
                for bit in range(8):
                    yy = y0 + 8 + bit
                    if yy < height_px and px[x, yy] == 0:
                        b1 |= (1 << (7 - bit))
                # bottom 8
                b2 = 0
                for bit in range(8):
                    yy = y0 + 16 + bit
                    if yy < height_px and px[x, yy] == 0:
                        b2 |= (1 << (7 - bit))
                band.extend((b0, b1, b2))

            # 3) Write band in small slices (avoid usblp choke)
            if self.bytes_per_write <= 0:
                f.write(band)
            else:
                i = 0
                step = self.bytes_per_write
                while i < len(band):
                    f.write(band[i:i+step])
                    i += step

            # 4) Explicit CRLF + dot-feed to “commit” this band on picky firmwares
            if self.send_crlf_after_band:
                f.write(b"\r\n")
            if self.band_feed_dots and self.band_feed_dots > 0:
                self._feed_dots(f, self.band_feed_dots)

            # 5) Force flush at the kernel/device level
            if self.fsync_each_band:
                os.fsync(f.fileno())

            if self.chunk_sleep > 0:
                time.sleep(self.chunk_sleep)
