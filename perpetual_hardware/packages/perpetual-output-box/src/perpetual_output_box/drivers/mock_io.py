from perpetual_output_box.drivers.base import AbstractIO, DoorState, LockState, BreakBeamState


class MockIO(AbstractIO):
    async def get_door_sensor(self) -> DoorState:
        return DoorState.OPEN

    async def get_break_beam_sensor(self) -> BreakBeamState:
        return BreakBeamState.OBSTRUCTED

    async def get_lock(self) -> LockState:
        return LockState.UNLOCKED

    async def set_door_locked(self, door_locked: LockState) -> None:
        print(f"Door Locked: {door_locked}")
        return None
