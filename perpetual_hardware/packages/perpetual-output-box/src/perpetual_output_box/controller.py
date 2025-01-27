from perpetual_trinamic.base import TrinamicTransporter
from perpetual_trinamic.tmcl import TmclReply, TmclMessage
from perpetual_output_box.io import AbstractIO, AbstractDoor, DoorConfiguration
from perpetual_trinamic.serial.trinamic_axis import TrinamicAxis
from typing import Optional
import asyncio
import serial_asyncio
from typing import Optional
import concurrent.futures
import gpiod #type: ignore


class TrinamicUsbConnection(TrinamicTransporter):

    def __init__(self, port: str, baudrate: int, loop: asyncio.AbstractEventLoop) -> None:
        self._port = port
        self._baudrate = baudrate
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._loop = loop
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=5)

    async def initialize(self) -> None:
        self._reader, self._writer = await serial_asyncio.open_serial_connection(
            url=self._port, baudrate=self._baudrate
        )

    async def read(self) -> bytes:
        if not self._reader:
            raise ConnectionError("Serial connection is not established. Call connect() first.")
        data = await self._reader.readexactly(9)
        return data

    async def shutdown(self) -> None:
        if self._writer:
            self._writer.close()
            await self._writer.wait_closed()

    async def send_message(
        self, message: TmclMessage, timeout: Optional[float] = 1.0
    ) -> TmclReply: 
        if not self._writer:
            raise ConnectionError("Serial connection is not established. Call connect() first.")
        await self._loop.run_in_executor(self._executor, self._writer.write, bytes(message.into_serial_buffer()))
        # TODO: FLUSH BUFFER ON FAILED READ
        async with asyncio.timeout(timeout):
            raw_reply = await self.read()
            return TmclReply.from_serial_buffer(raw_reply)


class TrinamicDoor(AbstractDoor):

    def __init__(self, transport: TrinamicTransporter, configuration: DoorConfiguration) -> None:
        self._transport = transport
        self._configuration = configuration
        self._axis = TrinamicAxis(transport, configuration.axis_configuration, configuration.module_id, configuration.motor_id) 

    async def initialize(self) -> None:
        await self._axis.initialize()

    async def open_door(self) -> None: 
        await self._axis.move_to_position(self._configuration.open_position)

    async def close_door(self) -> None:
        await self._axis.move_to_position(self._configuration.closed_position)

    async def axis(self) -> TrinamicAxis:
        return self._axis

    async def home(self) -> None:
        await self._axis.home()


class RpiIO(AbstractIO):

    def __init__(self, 
        chip_name: str ='gpiochip0', 
        door_pin: int = 5, 
        banner_pin: int = 17, 
        lock_pin: int = 22
        ) -> None:
        self._chip = gpiod.Chip(chip_name)
        self._door_pin = door_pin
        self._banner_pin = banner_pin
        self._lock_pin = lock_pin

        self._door_line = None
        self._banner_line = None
        self._lock_line = None
        self._setup_gpio()

    def _setup_gpio(self) -> None:
        """Configure input and output lines"""
        try:
            # Configure input pins with pull-up
            input_req = gpiod.line_request()
            input_req.consumer = "banner"
            input_req.request_type = gpiod.line_request.DIRECTION_INPUT

            self._banner_line = self._chip.get_line(self._banner_pin)
            self._banner_line.request(input_req)

            input_req = gpiod.line_request()
            input_req.consumer = "door"
            input_req.request_type = gpiod.line_request.DIRECTION_INPUT

            self._door_line = self._chip.get_line(self._door_pin)
            self._door_line.request(input_req)

            output_req = gpiod.line_request()
            output_req.consumer = "lock"
            output_req.request_type = gpiod.line_request.DIRECTION_OUTPUT

            self._lock_line = self._chip.get_line(self._lock_pin)
            self._lock_line.request(output_req, default_val=0)

        except Exception as e:
            self.close()
            raise RuntimeError(f"GPIO setup failed: {str(e)}")

    def get_door_sensor(self) -> bool: 
        if self._door_line:
            return self._door_line.get_value()
        raise Exception("Door Line not Initialized")

    def get_break_beam_sensor(self) -> bool: 
        if self._banner_line:
            return self._banner_line.get_value()
        raise Exception("Banner Line not Initialized")

    def set_door_locked(self, door_locked: bool) -> None:
        if self._lock_line:
            self._lock_line.set_value(1 if door_locked else 0)
        raise Exception("Lock Line not Initialized")

    def close(self):
        if self._door_line:
            self._door_line.release()
        if self._banner_line:
            self._banner_line.release()
        if self._lock_line:
            self._lock_line.release()
        self._chip.close()


class OutputBox:

    def __init__(self, door: AbstractDoor, io: AbstractIO) -> None:
        self._door = door
        self._io = io

    @property
    def door(self) -> AbstractDoor:
        return self._door

    @property
    def io(self) -> AbstractIO:
        return self._io
