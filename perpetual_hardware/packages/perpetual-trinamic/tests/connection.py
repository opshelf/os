#from can import Bus
from hardware_common.stepper_motor import (
    StepperMotorConfig,
    Rotation,
)
import time
import asyncio
#from perpetual_trinamic.trinamic_axis import TrinamicAxis
from perpetual_trinamic.serial.trinamic_axis import TrinamicAxis
from perpetual_trinamic.serial.connection import TrinamicUsbConnection


#def test_axis() -> None:
#    with Bus(interface="pcan", channel="PCAN_USBBUS1", bitrate=1000000, fd=False) as bus:
#        axis1 = TrinamicAxis(
#            bus,
#            module_id=100,
#            motor_id=0,
#            configuration=StepperMotorConfig(
#                microsteps=16,
#                steps_per_rev=3200,
#                encoder_ppr=16384,
#                steps_per_mm=1600,
#                work_current=1400,
#                holding_current=1000,
#                homing_current=500,
#                velocity=50,
#                acceleration=200,
#                max_velocity=100,
#                max_acceleration=100,
#                min_travel=0,
#                max_travel=185,
#                invert_coordinates=False,
#                home_direction=Rotation.CCW,
#                homing_speed=50,
#            ),
#        )
#        time.sleep(1)
#        axis1.move_to_position(100)


async def test_serial_axis() -> None:
        usb_conn = await TrinamicUsbConnection.build(
            "/dev/ttyACM0",
            115200,
            asyncio.get_event_loop()
        )
        axis1 = TrinamicAxis(
            usb_conn,
            module_id=1,
            motor_id=0,
            configuration=StepperMotorConfig(
                microsteps=16,
                steps_per_rev=200,
                encoder_ppr=4000,
                steps_per_mm=200,
                work_current=2500,
                holding_current=2000,
                homing_current=500,
                velocity=1000,
                acceleration=1000,
                max_velocity=100,
                max_acceleration=100,
                min_travel=0,
                max_travel=0,
                invert_coordinates=False,
                home_direction=Rotation.CCW,
                homing_speed=50,
                brake=True,
                external_encoder=True,
                max_encoder_deviation=500
            ),
            max_current=6000
        )
        await axis1.initialize()
        await axis1.set_pullup_resistors(0)
        await asyncio.sleep(1)
        #await axis1.init_brake_move()
        #await axis1.move_to_position(0)
        #encoder = await axis1.get_external_encoder_postion()
        #a_position = await axis1.get_actual_position()
        #t_position = await axis1.get_target_position()
        #print(encoder, a_position, t_position)
        #await axis1.safety_off()
        ##await axis1.move_to_relative_position(-10)
        #await axis1.init_brake_move()
        #await axis1.move_relative(-3200)
        #await asyncio.sleep(2)
        #encoder = await axis1.get_external_encoder_postion()
        #a_position = await axis1.get_actual_position()
        #t_position = await axis1.get_target_position()
        #print(encoder, a_position, t_position)
    
        #await axis1.init_brake_move()
        #for i in range(0, 10):
        #    await axis1.move_relative(3200)
        #    await asyncio.sleep(2)
        #    encoder = await axis1.get_external_encoder_postion()
        #    a_position = await axis1.get_actual_position()
        #    t_position = await axis1.get_target_position()
        #    print(encoder, a_position, t_position)
        #    await axis1.move_relative(-3200)
        #    await asyncio.sleep(2)
        #    encoder = await axis1.get_external_encoder_postion()
        #    a_position = await axis1.get_actual_position()
        #    t_position = await axis1.get_target_position()
        #    print(encoder, a_position, t_position)
        #await axis1.end_brake_move()
        for i in range(0, 10):
            await axis1.move_to_relative_position(-100)
            encoder = await axis1.get_external_encoder_postion()
            a_position = await axis1.get_actual_position()
            t_position = await axis1.get_target_position()
            print(i, encoder, a_position, t_position)
            await axis1.move_to_relative_position(100)
            encoder = await axis1.get_external_encoder_postion()
            a_position = await axis1.get_actual_position()
            t_position = await axis1.get_target_position()
            print(i, encoder, a_position, t_position)
        #try:
        #    await axis1.move_to_relative_position(100)
        #except Exception as e:
        #    print("Here")
        #    print(e)
        await axis1.end_brake_move()



if __name__ == "__main__":
    #test_axis()
    asyncio.run(test_serial_axis())


"""
1: BLACK - EA+
2: RED - VCC
3: WHITE - GND
11: YELLOW - EB+
12: GREEN - EB- 
13: BLUE - EA-

"""
