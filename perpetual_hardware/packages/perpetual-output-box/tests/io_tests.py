from serial_asyncio import asyncio
from perpetual_output_box.drivers.base import LockState
from perpetual_output_box.drivers.rpi_io import RpiIO
import time

async def main() -> None:
    rpi_io = RpiIO()
    state = LockState.LOCKED
    while True:
        print(
            await rpi_io.get_door_sensor(),
            await rpi_io.get_break_beam_sensor()
        ) 
        await rpi_io.set_door_locked(state)
        state = LockState.UNLOCKED if state == LockState.LOCKED else LockState.LOCKED
        time.sleep(1)

if __name__ == '__main__':
    asyncio.run(main())

