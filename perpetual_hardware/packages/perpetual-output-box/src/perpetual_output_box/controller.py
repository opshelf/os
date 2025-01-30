from perpetual_trinamic.serial.trinamic_axis import AxisStatus
from pydantic import BaseModel
from perpetual_output_box.drivers.base import (
    AbstractIO,
    AbstractDoor,
    DoorState,
    LockState,
    BreakBeamState,
    DoorConfiguration,
)
from perpetual_output_box.events import (
    OutputBoxCommands,
    SetLockCommand,
    OpenDoorCommand,
    CloseDoorCommand,
    InitializeCommand,
    StopCommand,
    HomeCommand,
    ReceiveItemCommand,
)
import asyncio


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
            lock_state=lock_state,
        )

    async def close_door(self) -> None:
        await self._door.close_door()

    async def lock_door(self) -> None:
        await self._io.set_door_locked(LockState.LOCKED)

    async def unlock_door(self) -> None:
        await self._io.set_door_locked(LockState.UNLOCKED)

    async def get_lock_state(self) -> LockState:
        lock_state = await self._io.get_lock()
        return lock_state

    async def get_door_state(self) -> DoorState:
        door_sensor = await self._io.get_door_sensor()
        return door_sensor

    async def get_break_beam_state(self) -> BreakBeamState:
        beam_sensor = await self._io.get_break_beam_sensor()
        return beam_sensor

    async def handle_command(self, command: OutputBoxCommands) -> None:
        match command:
            case SetLockCommand(payload=payload):
                await self._io.set_door_locked(payload)
            case OpenDoorCommand():
                await self._door.open_door()
            case CloseDoorCommand():
                await self._door.close_door()
            case InitializeCommand():
                await self.initialize()
            case StopCommand():
                await self._door.stop()
            case HomeCommand():
                await self._door.home()
            case ReceiveItemCommand(payload=payload):
                await self.receive_item()
            case _:
                raise Exception("Invalid Output Command")
