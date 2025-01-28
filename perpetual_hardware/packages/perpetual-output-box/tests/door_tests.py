import asyncio
from perpetual_output_box.door import TrinamicUsbConnection, TrinamicDoor, DoorConfiguration
from hardware_common.stepper_motor import StepperMotorConfig, Rotation
from perpetual_output_box.utils import get_port

async def main() -> None:
    stepper_config=StepperMotorConfig(
            microsteps=16,
            steps_per_rev=200,
            encoder_ppr=3200,
            steps_per_mm=80.0,
            work_current=2000,
            holding_current=0,
            homing_current=750,
            velocity=300,
            acceleration=150,
            max_velocity=200,
            max_acceleration=200,
            min_travel=None,
            max_travel=None,
            invert_coordinates=False,
            home_direction=Rotation.CCW,
            homing_speed=50,
        )
    door_config = DoorConfiguration(
        open_position=50.0,
        closed_position= 1.0,
        axis_configuration=stepper_config,
        module_id=1,
        motor_id=0
    )
    port = get_port()
    conn = TrinamicUsbConnection(
        port=port.device, 
        baudrate=115200,
        loop=asyncio.get_event_loop()
    )
    await conn.initialize()
    door = TrinamicDoor(
        conn,
        door_config
    )
    await door.initialize()
    await door.home()
    await door.open_door()

if __name__ == '__main__':
    asyncio.run(main())

