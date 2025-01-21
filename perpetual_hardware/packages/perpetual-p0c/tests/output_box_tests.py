from perpetual_p0c.utils import get_can_bus
from perpetual_p0c.output_box import OutputBox, OutputBoxConfiguration
from hardware_common.stepper_motor import  StepperMotorConfig, Rotation
from perpetual_p0c.common import CanAxisConfiguration, CanMessageIds
import asyncio

async def main() -> None:
    can_bus = await get_can_bus()
    p0_output_box_2_configuration = OutputBoxConfiguration(
        door=CanAxisConfiguration(
            can_id=CanMessageIds.OUTPUT_BOX_2,
            configuration=StepperMotorConfig(
                microsteps=16,
                steps_per_rev=400,
                encoder_ppr=3200,
                steps_per_mm=266.66666,
                work_current=2000,
                holding_current=0,
                homing_current=1000,
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
        door_open_position=205.0,
    )
    output_box = OutputBox(can_bus, p0_output_box_2_configuration)
    await output_box.initialize()
    await output_box.home()
    await output_box.open()





if __name__ == "__main__":
    asyncio.run(main())
