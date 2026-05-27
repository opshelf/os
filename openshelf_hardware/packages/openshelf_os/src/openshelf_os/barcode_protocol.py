# barcode_service.py
import asyncio
import logging
import serial_asyncio
import serial.tools.list_ports

logger = logging.getLogger(__name__).getChild("barcode")

class _BarcodeProtocol(asyncio.Protocol):
    def __init__(self, on_line):
        self.on_line = on_line
        self._buf = bytearray()

    def data_received(self, data: bytes) -> None:
        self._buf.extend(data)
        # accept either \n or \r as terminators
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
        # Let the service know the transport closed; reconnect loop handles it.
        logger.warning(
            "Barcode scanner connection lost",
            extra={"error": str(exc) if exc else None},
        )


class BarcodeService:
    """
    Usage:
        svc = BarcodeService(port="COM3")  # or /dev/ttyUSB0, or leave None to auto-pick
        await svc.start()
        async for code in svc:
            logging.info(code)
        await svc.stop()
    """
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

    # ---------- public API ----------
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

    async def get(self):  # await a single barcode
        return await self.queue.get()

    def __aiter__(self):  # async iterator of barcodes
        return self

    async def __anext__(self):
        try:
            item = await self.get()
            return item
        except asyncio.CancelledError:
            raise StopAsyncIteration

    # ---------- internal ----------
    async def _run(self):
        backoff = 0.5
        max_backoff = 8.0
        loop = asyncio.get_running_loop()

        def on_line(s: str):
            # Non-blocking: try put_nowait; if full, drop oldest to avoid deadlock
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
                coro = serial_asyncio.create_serial_connection(
                    loop, lambda: _BarcodeProtocol(on_line), port, baudrate=self.baudrate
                )
                self._transport, self._protocol = await coro
                # Connected; reset backoff
                backoff = 0.5
                logger.info("Barcode scanner connected", extra={"port": port})
                # Wait until transport closes or stop requested
                while not self._stop_evt.is_set():
                    await asyncio.sleep(0.2)
                break
            except asyncio.CancelledError:
                break
            except Exception as exc:
                # Connection failed or lost; back off and retry
                logger.warning(
                    "Barcode scanner connection failed; retrying",
                    extra={"port": self.port or "auto", "backoff_s": backoff, "error": str(exc)},
                )
                await asyncio.sleep(backoff)
                backoff = min(max_backoff, backoff * 2)
            finally:
                if self._transport:
                    self._transport.close()
                    self._transport = None

    def _choose_port(self) -> str:
        ports = list(serial.tools.list_ports.comports())
        if not ports:
            logger.error("No serial ports found for barcode scanner")
            raise RuntimeError("No serial ports found.")
        # Prefer by VID/PID if provided
        if self.prefer_vid_pid:
            vid, pid = self.prefer_vid_pid
            for p in ports:
                if getattr(p, "vid", None) == vid and getattr(p, "pid", None) == pid:
                    return p.device
        # Otherwise pick first CDC/ACM/USB-y one, else first
        for p in ports:
            desc = (p.description or "").lower()
            if any(k in desc for k in ("usb", "serial", "uart", "acm")):
                return p.device
        return ports[0].device
