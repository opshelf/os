from perpetual_p0c.utils import get_can_bus
from perpetual_p0c.output_box import OutputBox, OutputBoxConfiguration
from hardware_common.stepper_motor import  StepperMotorConfig, Rotation
from perpetual_p0c.common import CanAxisConfiguration, CanMessageIds
from perpetual_p0c.configuration import p0_output_box_1_configuration
import asyncio

async def main() -> None:
    can_bus = await get_can_bus()

    p0_output_box_2_configuration = OutputBoxConfiguration(
        door=CanAxisConfiguration(
            can_id=CanMessageIds.OUTPUT_BOX_2,
            configuration=StepperMotorConfig(
                microsteps=16,
                steps_per_rev=200,
                encoder_ppr=3200,
                steps_per_mm=400.0,
                work_current=2000,
                holding_current=0,
                homing_current=1500,
                velocity=100,
                acceleration=100,
                max_velocity=200,
                max_acceleration=200,
                min_travel=None,
                max_travel=None,
                invert_coordinates=False,
                home_direction=Rotation.CCW,
                homing_speed=100,
            ),
            name="output_box",
            axis_type="leadscrew",
        ),
        door_open_position=200.0,
    )
    p0_output_box_3_configuration = OutputBoxConfiguration(
        door=CanAxisConfiguration(
            can_id=CanMessageIds.OUTPUT_BOX_3,
            configuration=StepperMotorConfig(
                microsteps=16,
                steps_per_rev=200,
                encoder_ppr=3200,
                steps_per_mm=400.0,
                work_current=2000,
                holding_current=0,
                homing_current=1500,
                velocity=100,
                acceleration=100,
                max_velocity=200,
                max_acceleration=200,
                min_travel=None,
                max_travel=None,
                invert_coordinates=False,
                home_direction=Rotation.CCW,
                homing_speed=100,
            ),
            name="output_box",
            axis_type="leadscrew",
        ),
        door_open_position=200.0,
    )
    output_box_1 = OutputBox(can_bus, p0_output_box_1_configuration)
    output_box_2 = OutputBox(can_bus, p0_output_box_2_configuration)
    output_box_3 = OutputBox(can_bus, p0_output_box_3_configuration)
    await output_box_1.initialize()
    await output_box_2.initialize()
    await output_box_3.initialize()

    await output_box_1.home()
    await output_box_2.home()
    await output_box_3.home()

    for i in range(0, 10):
        ob1 = asyncio.create_task(output_box_1.open())
        ob2 = asyncio.create_task(output_box_2.open())
        ob3 = asyncio.create_task(output_box_3.open())
        await ob1
        await ob2
        await ob3
        ob1 = asyncio.create_task(output_box_1.close())
        ob2 = asyncio.create_task(output_box_2.close())
        ob3 = asyncio.create_task(output_box_3.close())
        await ob1
        await ob2
        await ob3



    #await output_box.home()
    #await output_box.open()





if __name__ == "__main__":
    asyncio.run(main())
