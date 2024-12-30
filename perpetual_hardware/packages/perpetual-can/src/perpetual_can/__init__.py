import asyncio
from can.notifier import Notifier
from typing import List, Optional, Any, Type, TypeVar

from can.bus import BusABC
from can.listener import AsyncBufferedReader, Listener
from can.message import Message
from inspect import Traceback

E = TypeVar("E", bound=BaseException)

class CANBusHandler:
    def __init__(self, bus: BusABC, loop: asyncio.AbstractEventLoop):
        self._bus = bus
        self._lock = asyncio.Lock()  # Lock to ensure safe access to the CAN bus
        self._loop = loop
        self._notifier = Notifier(bus, [], loop=loop)

    @property
    def bus(self) -> BusABC:
        return self._bus


    def __aenter__(self) -> "CANBusHandler":
        return self

    def __aexit__(self,
        exc_type: Optional[Type[E]],
        exc_val: Optional[E],
        exc_tb: Optional[Traceback],
        ) -> None:
        print(exc_type, exc_val, exc_tb)
        self.shutdown()

    def __enter__(self) -> "CANBusHandler":
        return self

    def __exit__(self,
        exc_type: Optional[Type[E]],
        exc_val: Optional[E],
        exc_tb: Optional[Traceback],
        ) -> None:
        print(exc_type, exc_val, exc_tb)
        self.shutdown()

    async def send_message(self, message: Message, timeout: Optional[float] = None) -> None:
        """Send a CAN message."""
        async with self._lock:
            self._bus.send(message, timeout)

    async def recv_message(self, timeout: Optional[float] = None) -> Optional[Message]:
        """Receive a CAN message."""
        async with self._lock:
            message = self._bus.recv(timeout)
            return message

    def add_listener(self, listener: Listener) -> None:
        self._notifier.add_listener(listener)

    def remove_listener(self, listener: Listener) -> None:
        self._notifier.remove_listener(listener)

    def shutdown(self) -> None:
        self._bus.shutdown()

    @property
    def notifier(self) -> Notifier:
        return self._notifier


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
