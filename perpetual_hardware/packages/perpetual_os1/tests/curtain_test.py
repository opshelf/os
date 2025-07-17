from typing import Optional, List
from pydantic import BaseModel
from pymodbus.client.serial import AsyncModbusSerialClient
from perpetual_os1.curtain import IntelligentGrating
from perpetual_os1.door import Door, DoorConfiguration
from perpetual_os1.utils import get_can_bus, get_modbus_client
import asyncio
from perpetual_os1.common import CanMessageIds, CanAxisConfiguration
from hardware.stepper_motor import StepperMotorConfig, Rotation
from perpetual_trinamic.aio.trinamic_axis import AxisStatus, TrinamicAxis

curtain_axis_configuration = CanAxisConfiguration(
    can_id=CanMessageIds.CURTAIN.value,
    #can_id=CanMessageIds.DOOR.value,
    configuration=StepperMotorConfig(
        microsteps=16,
        steps_per_rev=200,
        encoder_ppr=4000,
        steps_per_mm=400.0,
        work_current=2000,
        holding_current=0,
        homing_current=1000,
        velocity=100,
        acceleration=100,
        max_velocity=200,
        max_acceleration=50,
        min_travel=None,
        max_travel=277,
        invert_coordinates=True,
        home_direction=Rotation.CW,
        homing_speed=50,
        external_encoder=True,
        brake=True

    ),
    name="door",
    axis_type="leadscrew",
)




class LocalizedResult(BaseModel):
    object_size: float
    first_break_location: float
    last_break_location: float

class Localizer:

    def __init__(self, beam_distance_mm: float, deadspace: float, reverse: bool = True) -> None:
        self._beam_distance_mm = beam_distance_mm
        self._deadspace = deadspace
        self._reverse = reverse
        self._padding = self._beam_distance_mm * 2

    def localize(self, beams: List[int]) -> Optional[LocalizedResult]:
        first_break = None
        last_break = None
        if self._reverse:
            beams.reverse()
        for idx, beam in enumerate(beams):
            if first_break is None:
                if beam:
                    first_break = idx
            else:
                if beam:
                    last_break = idx
        if first_break is None:
            return None
        elif first_break is not None and last_break is not None:
            if first_break == 0:
                return LocalizedResult(
                    object_size=(last_break-first_break)*self._beam_distance_mm + self._deadspace + self._padding/2,
                    first_break_location=first_break*self._beam_distance_mm + self._deadspace,
                    last_break_location=last_break*self._beam_distance_mm + self._deadspace + self._padding/2
                )
            else:
                return LocalizedResult(
                    object_size=(last_break-first_break)*self._beam_distance_mm + self._padding,
                    first_break_location=first_break*self._beam_distance_mm + self._deadspace  - self._padding/2,
                    last_break_location=last_break*self._beam_distance_mm + self._deadspace + self._padding/2
                )
        elif first_break is not None:
            if first_break == 0:
                return LocalizedResult(
                    object_size=first_break*self._beam_distance_mm + self._deadspace + self._padding/2,
                    first_break_location=first_break*self._beam_distance_mm + self._deadspace,
                    last_break_location=self._beam_distance_mm + self._deadspace
                )
            else:
                return LocalizedResult(
                    object_size=20,
                    first_break_location=first_break*self._beam_distance_mm + self._deadspace + self._padding,
                    last_break_location=first_break*self._beam_distance_mm + self._deadspace,
                )
        else: 
            return None

async def light_curtain_data(client:AsyncModbusSerialClient) -> None:
    lc = IntelligentGrating(client,address=0x40)
    localizer = Localizer(5.0, 20.0)
    beams = await lc.read_all_light_status(31)
    if beams: 
        print(localizer.localize(beams))
    #qty = await lc.read_blocked_quantity()
    #highest = await lc.read_highest_blocked()
    #lowest = await lc.read_lowest_blocked()
    #if highest is not None and lowest is not None and qty is not None:
    #    print(highest - lowest, qty, (highest - lowest) * 5)

async def run_size_routine(axis: TrinamicAxis, lc: IntelligentGrating) -> None:
    localizer = Localizer(5.0, 25.0)
    for i in [0, 20, 70, 120, 170, 220]:
        await axis.move_to_position(i)
        beams = await lc.read_all_light_status(31)
        if beams: 
            result = localizer.localize(beams)
            if result:
                print(result, i + 30.0)
            else:
                print("Height: ",i + 30.0)
                break
    await axis.move_to_position(0)


async def run_size_scan(axis: TrinamicAxis, lc: IntelligentGrating) -> None:
    localizer = Localizer(5.0, 25.0)
    localizations: List[LocalizedResult] = []
    try:
        await axis.move_to_position(0)
        await axis.init_brake_move()
        await axis.move_to_position(220, override_brake=True)
        while True:
            beams = await lc.read_all_light_status(31)
            position = await axis.get_position()
            if beams: 
                result = localizer.localize(beams)
                if result:
                    localizations.append(result)
                    pass
                else:
                    print("Height: ", position + 30.0)
                    await axis.stop()
                    break
            position_reached = await axis.get_position_reached()
            if position_reached:
                break
        await axis.move_to_position(0)
        max_object_size = 0.0
        min_bound = 200
        max_bound = 0
        for localization in localizations:
            if localization.object_size > max_object_size:
                max_object_size = localization.object_size
            if localization.first_break_location < min_bound:
                min_bound = localization.first_break_location
            if localization.last_break_location > max_bound:
                max_bound = localization.last_break_location
        print(max_object_size, min_bound, max_bound)
            
    finally:
        await axis.end_brake_move()



async def main() -> None:
    can_bus = await get_can_bus()
    modbus_client = await get_modbus_client()
    curtain = TrinamicAxis(
            can_bus,
            configuration=curtain_axis_configuration.configuration,
            module_id=curtain_axis_configuration.can_id,
    )
    await curtain.initialize()
    #await curtain.home()
    #input()
    lc = IntelligentGrating(modbus_client,address=0x40)
    #await run_size_routine(curtain, lc)
    await run_size_scan(curtain, lc)
    #while True:
    #    await light_curtain_data(modbus_client)
    #    await asyncio.sleep(.1)
    #lc = IntelligentGrating(modbus_client,address=0x40)
    #print(await lc.read_all_light_status())
    #await curtain.initialize()
    #await curtain.home()
    #for i in range(0, 10):
    #    await curtain.init_brake_move()
    #    input()
    #    await curtain.end_brake_move()
    #    input()
    #await curtain.init_brake_move()
    #try:
    #    await curtain.move_to_position(1000, override_brake=True)
    #    await curtain.wait_for_move()
    #except Exception as e:
    #    print(e)
    #await curtain.end_brake_move()
    #await curtain.move_to_position(0)
    #await curtain.move_to_position(277)
    #await curtain.move_to_position(0)
    #await curtain.move_to_position(277)
    #print(await curtain.get_position())



if __name__ == "__main__":
    asyncio.run(main())
