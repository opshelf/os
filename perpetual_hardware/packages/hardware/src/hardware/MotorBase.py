import hardware.MotorDefs as MotorDefs
from hardware.ModbusController import SharedModbusController
import time
import asyncio
from enum import Enum

class MotorStatus(Enum):
    IDLE = 0
    MOVING = 1
    POSITION_REACHED = 2
    ERROR = 3

class Servo():
    def __init__(self, controller: SharedModbusController):
            # self._motor_id = int(params["motor_id"])        # slave id
            self._controller = controller                   # modbus controller
            # self._params = params 

    # async def clear(self):
    #     await self._controller.write_single_register(9, 0x3E8, 0x0)

    async def configure(self):
        await self._controller.write_single_register(9, 0x3E8, 0x0) #clear
        await self._controller.write_single_register(9, 0x3E8, 0x1) #enable
        await self.wait_for_activate()
        #open
        await self._controller.write_single_register(9, 0x03EB, 0x8020) # 0x80|0x05
        await self._controller.write_single_register(9, 0x03EC, 0x80)
        #close
        await self._controller.write_single_register(9, 0x03ED, 0x80FE) # 0x80|0xFF
        await self._controller.write_single_register(9, 0x03EE, 0x80)
        #safe open
        await self._controller.write_single_register(9, 0x03EF, 0x80B0) # 0x80|0xFF
        await self._controller.write_single_register(9, 0x03F0, 0x80)

    async def open(self):
        await self._controller.write_single_register(9, 0x3E8, 0x080B) #clear
        await self.wait_for_servo()

    async def safe_open(self):
        await self._controller.write_single_register(9, 0x3E8, 0x0A0B) #clear
        await self.wait_for_servo()

    async def close(self):
        await self._controller.write_single_register(9, 0x3E8, 0x090B) #clear
        await self.wait_for_servo()

    async def wait_for_activate(self):
        start_time = time.monotonic()

        while True:
            status = await self._controller.read_holding_registers(9,0x07D0) & (1<<5)
            # print(bin(status))
            if status is None:
                print("Failed to read activation status.")
                return MotorStatus.ERROR

            if status:
                # print(f"[gripper] Activation complete.")
                return 

            if time.monotonic() - start_time > 10:
                print(f"[gripper] Timeout waiting for activation.")
                return MotorStatus.ERROR

            await asyncio.sleep(0.01)


    async def wait_for_servo(self):
        start_time = time.monotonic()

        while True:
            status = await self._controller.read_holding_registers(9, 0x07D0)
            status = (status & (1<<6)) or (status & (1<<7))
            # print(hex(status))
            if status is None:
                print("Failed to read gripper status.")
                return MotorStatus.ERROR

            if status:
                print(f"[gripper] Move complete.")
                return 

            if time.monotonic() - start_time > 5.0:
                print(f"[gripper] Timeout waiting for move.")
                return MotorStatus.ERROR

            await asyncio.sleep(0.01)
         

# stepper
class Motor():
    #TODO change to absolute positioning mode w/ encoder feedback

    def __init__(self, controller: SharedModbusController, params: dict):
            self._motor_id = int(params["motor_id"])        # slave id
            self._controller = controller                   # modbus controller
            self._params = params
            self._cur_pos = None
            self._expected_pos = None
            # self._pos_tol = 100 # pulses
            self._stall_timeout = 0.05 #second??? 
            self._homed = False 
            self._crashed = False
            self._cur_dir = 0

    async def configure(self):
        await self.set_param("Forced enable by software", 0x1) # always on?
        await self.set_param("SI2 input function", 0x26) # neg switch
        await self.set_param("Peak current", self._params["Ip"])
        await self.set_param("Allowed max position following error", 4000)
        return

    # this sort of assumes we're only using path 0, and will have to be adapted if we do anything fancy.
    async def move_to_position(self, value: int): # mm
        # if value > self._params["nrev"]:
        #      print(f"[Stepper] Motor {self._motor_id} -> Cannot Move to position {value}")
        #      return

        # if self._homed = 
        await self.set_param("Control word", 0x1111) # clear alarm if any
        await self.set_param("Control word", 0x1122) # clear alarm if any

        pulse = MotorDefs.rev_to_pulse(MotorDefs.mm_to_rev(value,self._params["mpr"])) # tuple

        await self.set_param("Motion of Path 0", 0x1) # abs pos
        await self.set_param("Motor direction", self._params["dir"])
        await self.set_param("Velocity", self._params["rpm"])
        await self.set_param("Acceleration", self._params['accel'])
        await self.set_param("Deceleration", self._params['accel'])
        await self.set_param("Position High", pulse[0])
        await self.set_param("Position Low", pulse[1])
        await self._controller.write_single_register(
                self._motor_id, 
                MotorDefs.TRIGGER,
                0x10) # start path 0
        
        self._homed = False
        await self.wait_for_move(timeout=10)
        return

    # TODO add actual position registers to the motor defs table
    async def get_position(self):
        # todo get_param
        pos_l = await self._controller.read_holding_registers(self._motor_id, 0x602D) 
        pos_h = await self._controller.read_holding_registers(self._motor_id, 0x602C)
        pos = (pos_h << 16) | pos_l
        if pos:
            # print(f"[Stepper] Motor {self._motor_id} -> Position: {pos}")
            self._cur_pos = pos
            return pos
        return None
    
    async def set_param(self, register, value):
        await self._controller.write_single_register(self._motor_id, 
                MotorDefs.get_register_by_name(register), 
                value)
    
    async def get_param(self, register) -> int:
        val = await self._controller.read_holding_registers(self._motor_id, 
                    MotorDefs.get_register_by_name(register))
        if val is not None:
            # print(f"[Stepper] Motor {self._motor_id} -> val: {val}")
            return int(val)
        return None 
    
    async def wait_for_move(self, timeout: float = 10.0) -> MotorStatus:
        start_time = time.monotonic()
        # await self.get_position()
        last_pos = None
        has_moved=False
        last_movement_time = start_time

        while True:
            status = await self.get_param("Motion status") & (1<<5)
            await self.get_position()
            now = time.monotonic()

            if status is None:
                print(f"[{self._motor_id}] Failed to read motion status.")
                return None
            
            if last_pos is not None and self._cur_pos != last_pos:
                last_movement_time = now
                has_moved = True
            elif last_movement_time is None:
                last_movement_time = now  # initialize once

            last_pos = self._cur_pos
            
            # todo this is also the case that catches the limit switch which stops the motor from actually moving
            if has_moved and (now - last_movement_time > self._stall_timeout):
                print(f"[{self._motor_id}] Stopped: No movement detected for {self._stall_timeout} seconds.")
                await self.stop()
                return 

            # todo move to within move operation?
            if status:
                # print(f"[{self._motor_id}] Move complete.")
                # await self.stop()
                return MotorStatus.POSITION_REACHED

            if time.monotonic() - start_time > timeout:
                print(f"[{self._motor_id}] Timeout waiting for move.")
                await self.stop()
                return MotorStatus.ERROR

            # print("waiting")
            await asyncio.sleep(0.01)

    # TODO set the flag (5 for motion, 6 for homing) as a parameter
    # async def wait_for_home(self, timeout: float = 10.0) -> MotorStatus:
        start_time = time.monotonic()
        # await self.get_position()
        last_pos = None
        has_moved=False
        last_movement_time = start_time

        while True:
            status = await self.get_param("Motion status") & (1<<6)
            await self.get_position()
            now = time.monotonic()

            if status is None:
                print(f"[{self._motor_id}] Failed to read motion status.")
                return MotorStatus.ERROR
            
            if last_pos is not None and self._cur_pos != last_pos:
                last_movement_time = now
                has_moved = True
            elif last_movement_time is None:
                last_movement_time = now  # initialize once

            last_pos = self._cur_pos
            
            if has_moved and (now - last_movement_time > self._stall_timeout):
                print(f"[{self._motor_id}] Homed: No movement detected for {self._stall_timeout} seconds.")
                self._homed = True
                await self.stop()
                return

            # todo move to within move operation?
            if status:
                print(f"[{self._motor_id}] Home complete.")
                self._homed = True
                return MotorStatus.POSITION_REACHED

            if time.monotonic() - start_time > timeout:
                print(f"[{self._motor_id}] Timeout waiting for home.")
                await self.stop()
                return MotorStatus.ERROR

            # print("waiting")
            await asyncio.sleep(0.01)

    async def home(self):

        await self.set_param("Control word", 0x1111) # clear alarm if any
        await self.set_param("Control word", 0x1122) # clear alarm if any

        # the homing direction register follows an inverted 
        # definition to the motor direction register
        # so this ends up being the opposite direction anyway
        home_dir = self._params["dir"]^1
        # if self._motor_id == 1:
        #     home_dir = (self._params["dir"])

        await self.set_param("Motion of Path 0", 0x2) # velocity
        await self.set_param("Motor direction", home_dir)
        await self.set_param("Velocity", self._params["home_rpm"])
        await self._controller.write_single_register(
                self._motor_id, 
                MotorDefs.TRIGGER,
                0x10) # start path 0
        
        await self.wait_for_move(timeout=60)
        # if home successful
        await self._controller.write_single_register(
            self._motor_id,
            MotorDefs.TRIGGER,
            0x21) # set zero position
        
    # lazy retraction method 
    async def retract(self):
        await self.set_param("Control word", 0x1111) # clear alarm if any
        await self.set_param("Control word", 0x1122) # clear alarm if any

        home_dir = self._params["dir"]^1

        await self.set_param("Motion of Path 0", 0x2) # velocity
        await self.set_param("Motor direction", home_dir)
        await self.set_param("Velocity", self._params["ret_rpm"]) # only use with gripper and slide
        await self._controller.write_single_register(
                self._motor_id, 
                MotorDefs.TRIGGER,
                0x10) # start path 0
        
        await self.wait_for_move(timeout=10)
        # # if home successful
        await self._controller.write_single_register(
            self._motor_id,
            MotorDefs.TRIGGER,
            0x21) # set zero position
        
    async def jog(self, dist):
        await self.set_param("Control word", 0x1111) # clear alarm if any
        await self.set_param("Control word", 0x1122) # clear alarm if any

        pulse = MotorDefs.rev_to_pulse(MotorDefs.mm_to_rev(dist,self._params["mpr"])) # tuple

        await self.set_param("Motion of Path 0", 0x41) # rel pos
        await self.set_param("Motor direction", self._params["dir"])
        await self.set_param("Velocity", self._params["rpm"])
        await self.set_param("Acceleration", self._params['accel'])
        await self.set_param("Deceleration", self._params['accel'])
        await self.set_param("Position High", pulse[0])
        await self.set_param("Position Low", pulse[1])
        await self._controller.write_single_register(
                self._motor_id, 
                MotorDefs.TRIGGER,
                0x10) # start path 0
        
        # self._homed = False
        await self.wait_for_move(timeout=10)

    async def stop(self):
        await self._controller.write_single_register(
        self._motor_id, 
        MotorDefs.TRIGGER,
        0x40)


