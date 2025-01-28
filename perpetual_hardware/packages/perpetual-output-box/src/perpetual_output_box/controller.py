from perpetual_trinamic.serial.trinamic_axis import AxisStatus
from pydantic import BaseModel
from perpetual_output_box.io import AbstractIO, AbstractDoor
from perpetual_output_box.door import DoorConfiguration
import asyncio
from enum import Enum

class DoorState(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"

class LockState(str, Enum):
    LOCKED = "LOCKED"
    UNLOCKED = "UNLOCKED"

class BreakBeamState(str, Enum):
    OBSTRUCTED = "OBSTRUCTED"
    UNOBSTRUCTED = "UNOBSTRUCTED"


class OutputBoxConfiguration(BaseModel):
    door_configuration: DoorConfiguration


class OutputBoxStatus(BaseModel):
    door_state: DoorState
    break_beam_state: BreakBeamState
    lock_state: LockState
    door: AxisStatus


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

    async def initialize(self) -> None:
        await self.initialize()

    async def receive_item(self) -> None:
        await self._door.open_door()
        await self.lock_door()
        beam_obstructed = False
        try:
            async with asyncio.timeout(20):
                while True:
                    door_state = await self.get_door_state()
                    beam_state = await self.get_break_beam_state()
                    if door_state == DoorState.OPEN:
                        raise Exception("Door Was Opened")
                    if beam_state == BreakBeamState.OBSTRUCTED:
                        beam_obstructed = True
                    if beam_obstructed and beam_state == BreakBeamState.UNOBSTRUCTED:
                        break
                    await asyncio.sleep(0.01)
            await asyncio.sleep(0.5)
            await self.close_door()
            await self.unlock_door()
        except asyncio.TimeoutError:
            await self.close_door()
            await self.unlock_door()

    async def status(self) -> OutputBoxStatus:
        door_state = await self.get_door_state()
        break_beam_state = await self.get_break_beam_state()
        door_status = await self._door.status()
        lock_state = await self.get_lock_state()
        return OutputBoxStatus(
            door_state=door_state,
            break_beam_state=break_beam_state,
            door=door_status,
            lock_state=lock_state
        )

    async def close_door(self) -> None:
        await self._door.close_door()

    async def lock_door(self) -> None:
        self._io.set_door_locked(True)

    async def unlock_door(self) -> None:
        self._io.set_door_locked(False)

    async def get_lock_state(self) -> LockState:
        """PNP NO OUTPUT"""
        door_sensor = self._io.get_lock()
        if door_sensor:
            return LockState.LOCKED
        else:
            return LockState.UNLOCKED

    async def get_door_state(self) -> DoorState:
        """PNP NO OUTPUT"""
        door_sensor = self._io.get_door_sensor()
        if door_sensor:
            return DoorState.CLOSED
        else:
            return DoorState.OPEN

    async def get_break_beam_state(self) -> BreakBeamState:
        """NPN NO OUTPUT"""
        beam_sensor = self._io.get_break_beam_sensor()
        if beam_sensor:
            return BreakBeamState.UNOBSTRUCTED
        else:
            return BreakBeamState.OBSTRUCTED

    
    async def handle_command(self, command: OutputBoxCommand) -> OutputBoxEvent:





