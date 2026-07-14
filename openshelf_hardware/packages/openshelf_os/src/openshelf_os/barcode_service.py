import asyncio
import logging
from pathlib import Path

import serial_asyncio
import serial.tools.list_ports

logger = logging.getLogger(__name__).getChild("barcode")

HIDRAW_ROOT = Path("/sys/class/hidraw")
DEFAULT_BARCODE_PORT = "/dev/serial/by-id/usb-Linux_4.9.84_Barcode_Scanner-if00"
SCANNER_NOT_DETECTED_REASON = "scanner not detected"
SCANNER_HID_MODE_REASON = "scanner is in usb hid keyboard mode; switch it to usb-com (serial) mode"


def find_hid_scanner_names(root: Path = HIDRAW_ROOT) -> list[str]:
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return []

    names: list[str] = []

    for entry in entries:
        try:
            uevent = (entry / "device" / "uevent").read_text()
        except OSError:
            continue

        for line in uevent.splitlines():
            if line.startswith("HID_NAME=") is False:
                continue

            name = line.split("=", 1)[1].strip()

            if "barcode" in name.lower() and name not in names:
                names.append(name)

    return names


def describe_missing_scanner(root: Path = HIDRAW_ROOT) -> str:
    hid_names = find_hid_scanner_names(root)

    if not hid_names:
        return SCANNER_NOT_DETECTED_REASON

    logger.warning(
        "Barcode scanner enumerated as USB HID keyboard instead of a serial device",
        extra={"hid_names": hid_names},
    )

    return SCANNER_HID_MODE_REASON


class _BarcodeProtocol(asyncio.Protocol):
    def __init__(self, on_line, on_connection_lost):
        self.on_line = on_line
        self.on_connection_lost = on_connection_lost
        self._buf = bytearray()

    def data_received(self, data: bytes) -> None:
        self._buf.extend(data)

        while True:
            nl = self._buf.find(b'\n')
            cr = self._buf.find(b'\r')
            cut = nl if (nl != -1 and (cr == -1 or nl < cr)) else cr

            if cut == -1:
                break

            line = self._buf[:cut]
            self._buf = self._buf[cut+1:]

            try:
                s = line.decode("utf-8", errors="replace").strip()
            except Exception:
                s = line.hex()

            if s:
                self.on_line(s)

    def connection_lost(self, exc):
        logger.warning(
            "Barcode scanner connection lost",
            extra={"error": str(exc) if exc else None},
        )
        self.on_connection_lost()


class BarcodeService:
    def __init__(self, port: str | None = None, baudrate: int = 115200,
                 queue_maxsize: int = 100, prefer_vid_pid: tuple[int,int] | None = None):
        self.port = port
        self.baudrate = baudrate
        self.prefer_vid_pid = prefer_vid_pid
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=queue_maxsize)
        self._transport = None
        self._protocol = None
        self._runner_task: asyncio.Task | None = None
        self._stop_evt = asyncio.Event()
        self._connected_evt = asyncio.Event()

    async def wait_connected(self, timeout: float) -> bool:
        if self._connected_evt.is_set():
            return True

        try:
            await asyncio.wait_for(self._connected_evt.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def start(self):
        if self._runner_task is not None:
            return

        logger.info(
            "Starting barcode service",
            extra={"port": self.port, "baudrate": self.baudrate},
        )

        self._stop_evt.clear()
        self._runner_task = asyncio.create_task(self._run(), name="barcode-runner")

    async def stop(self):
        self._stop_evt.set()

        if self._runner_task:
            self._runner_task.cancel()

            try:
                await self._runner_task
            except asyncio.CancelledError:
                pass

        self._runner_task = None

        if self._transport:
            self._transport.close()
            self._transport = None

        logger.info("Barcode service stopped")

    async def get(self):
        return await self.queue.get()

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.get()

    async def _run(self):
        backoff = 0.5
        max_backoff = 8.0
        loop = asyncio.get_running_loop()

        def on_line(s: str):
            if self.queue.full():
                try:
                    self.queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                self.queue.put_nowait(s)
            except asyncio.QueueFull:
                pass

        while not self._stop_evt.is_set():
            try:
                port = self._choose_port() if self.port is None else self.port

                logger.debug(
                    "Attempting barcode scanner connection",
                    extra={"port": port, "baudrate": self.baudrate},
                )

                connection_lost = asyncio.Event()

                coro = serial_asyncio.create_serial_connection(
                    loop,
                    lambda: _BarcodeProtocol(on_line, connection_lost.set),
                    port,
                    baudrate=self.baudrate,
                )

                self._transport, self._protocol = await coro
                backoff = 0.5
                self._connected_evt.set()

                logger.info("Barcode scanner connected", extra={"port": port})

                await connection_lost.wait()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(
                    "Barcode scanner connection failed; retrying",
                    extra={"port": self.port or "auto", "backoff_s": backoff, "error": str(exc)},
                )

                await asyncio.sleep(backoff)

                backoff = min(max_backoff, backoff * 2)
            finally:
                self._connected_evt.clear()
                if self._transport:
                    self._transport.close()
                    self._transport = None

    def _choose_port(self) -> str:
        ports = list(serial.tools.list_ports.comports())

        if not ports:
            logger.error("No serial ports found for barcode scanner")
            raise RuntimeError("No serial ports found.")

        if self.prefer_vid_pid:
            vid, pid = self.prefer_vid_pid

            for p in ports:
                if getattr(p, "vid", None) == vid and getattr(p, "pid", None) == pid:
                    return p.device

        for p in ports:
            desc = (p.description or "").lower()

            if any(k in desc for k in ("usb", "serial", "uart", "acm")):
                return p.device

        return ports[0].device
