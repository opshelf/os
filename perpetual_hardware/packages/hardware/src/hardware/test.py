import asyncio
import MotorBase
from MotorBase import Motor
from ModbusController import SharedModbusController as Controller

import time

async def main():
    mb = Controller(port="/dev/ttyUSB0")
    await mb.connect()

    ms = Motor(mb, MotorBase.motor_stage)
    mg = Motor(mb, MotorBase.motor_gripper)
    mr = Motor(mb, MotorBase.motor_rotate)
    mx = Motor(mb, MotorBase.motor_x)

    await mx.configure()
    await ms.configure()
    await mg.configure()
    await mr.configure()

    await ms.move_to_position(50)

    time.sleep(1)

    await mg.move_to_position(182)

    time.sleep(1)

    await mr.move_to_position(180)

    time.sleep(1)

    await mx.move_to_position(650) # should be two revolutions

    # await mb.close()
    

if __name__ == "__main__":
    asyncio.run(main())