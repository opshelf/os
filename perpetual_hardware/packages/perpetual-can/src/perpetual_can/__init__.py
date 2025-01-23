import asyncio
from can.notifier import Notifier
from typing import List, Optional, Any, Type, TypeVar

from can.bus import BusABC
from can.listener import AsyncBufferedReader, Listener
from can.message import Message
from inspect import Traceback
import logging
import concurrent.futures
import can

log = logging.getLogger(__name__)


E = TypeVar("E", bound=BaseException)


class AsyncFilteredReader(AsyncBufferedReader):
    def __init__(self, filters: List[int], **kwargs: Any) -> None:
        AsyncBufferedReader.__init__(self, **kwargs)
        self._filters = filters

    def on_message_received(self, msg: Message) -> None:
        if not self._is_stopped and msg.arbitration_id in self._filters:
            self.buffer.put_nowait(msg)

    async def get_message(self, timeout: Optional[float] = None) -> Message:
        if timeout is None:
            return await self.buffer.get()
        else:
            async with asyncio.timeout(timeout):
                return await self.buffer.get()

    async def clear_buffer(self) -> None:
        while not self.buffer.empty():
            self.buffer.get_nowait()
            self.buffer.task_done()

    def get_buffered_messages(self) -> int:
        return self.buffer.qsize()


class CANBusHandler:
    def __init__(self, bus: BusABC, loop: asyncio.AbstractEventLoop):
        self._bus = bus
        self._lock = asyncio.Lock()  # Lock to ensure safe access to the CAN bus
        self._loop = loop
        #self._printer = can.Printer()
        #self._notifier = Notifier(bus, [self._printer], loop=loop)
        self._notifier = Notifier(bus, [], loop=loop)
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=5)

    @property
    def bus(self) -> BusABC:
        return self._bus

    def __aenter__(self) -> "CANBusHandler":
        return self

    def __aexit__(
        self,
        exc_type: Optional[Type[E]],
        exc_val: Optional[E],
        exc_tb: Optional[Traceback],
    ) -> None:
        print(exc_type, exc_val, exc_tb)
        self.shutdown()

    def __enter__(self) -> "CANBusHandler":
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[E]],
        exc_val: Optional[E],
        exc_tb: Optional[Traceback],
    ) -> None:
        print(exc_type, exc_val, exc_tb)
        self.shutdown()

    async def send_ack_message(
        self, message: Message, reader: AsyncFilteredReader, timeout: Optional[float] = None
    ) -> Message:
        """Send a CAN message and lock the bus until the expected_id is received."""
        async with self._lock:
            await self._loop.run_in_executor(self._executor, self._bus.send, message, timeout)
            async with asyncio.timeout(1.0):
                msg = await reader.get_message()
                return msg

    async def recv_message(self, timeout: Optional[float] = None) -> Optional[Message]:
        """Receive a CAN message."""
        async with self._lock:
            message = self._bus.recv(timeout)
            if message is None:
                print("No Message Received")
                await asyncio.sleep(0.25)
            return message

    async def send_message(self, message: Message, timeout: Optional[float] = None) -> None:
        """Send a CAN message."""
        async with self._lock:
            await self._loop.run_in_executor(self._executor, self._bus.send, message, timeout)
            # self._bus.send(message, timeout)

    def add_listener(self, listener: Listener) -> None:
        self._notifier.add_listener(listener)

    def remove_listener(self, listener: Listener) -> None:
        self._notifier.remove_listener(listener)

    def shutdown(self) -> None:
        self._bus.shutdown()

    @property
    def notifier(self) -> Notifier:
        return self._notifier

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock
