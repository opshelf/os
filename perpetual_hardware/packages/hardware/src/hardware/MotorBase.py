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
    def __init__(self, controller: SharedModbusController, params: dict):
            # self._motor_id = int(params["motor_id"])        # slave id
            self._controller = controller                   # modbus controller
            # self._params = params 

    async def configure(self):
        # await self.set_param("Peak Current", int(self._params["Ip"]))
        await self._controller.write_single_register(9, 0x3E8, 0x3) #enable, 
    
    async def open(self):
         await (self._controller.write_single_register(9, 0x3E8, 0x103))
         await self.wait_for_servo()

    async def close(self):
         await (self._controller.write_single_register(9, 0x3E8, 0x203))
         await self.wait_for_servo()

    async def wait_for_servo(self):
        # start_time = time.monotonic()

        while True:
            status = await self._controller.read_holding_registers(9,0x07D0) & (1<<7)
            if status is None:
                # print(f"[{self.motor_id}] Failed to read motion status.")
                return MotorStatus.ERROR

            print(f"[gripper] Motion status: {bin(status)}")

            if status:
                print(f"[gripper] Move complete.")
                return 

            # if time.monotonic() - start_time > timeout:
            #     # print(f"[{self.motor_id}] Timeout waiting for move.")
            #     return MotorStatus.ERROR

            await asyncio.sleep(0.05)
         

# stepper
class Motor():

    def __init__(self, controller: SharedModbusController, params: dict):
            self._motor_id = int(params["motor_id"])        # slave id
            self._controller = controller                   # modbus controller
            self._params = params 

    async def configure(self):
        await self.set_param("Peak Current", int(self._params["Ip"]))
        await self.set_param("Velocity", int(self._params["rpm"]))
        await self.set_param("Motor direction", int(self._params["dir"]))

        return

    # this sort of assumes we're only using path 0, and will have to be adapted if we do anything fancy.
    async def move_to_position(self, value: int): # mm
        if value > self._params["nrev"]:
             print(f"[Stepper] Motor {self._motor_id} -> Cannot Move to position {value}")
             return
        # print(f"[Stepper] Motor {self._motor_id} -> Move to position {value}")

        pulse = MotorDefs.rev_to_pulse(MotorDefs.mm_to_rev(value,self._params["mpr"])) # tuple

        await self.set_param("Motion of Path 0", 0x41) # position mode
        await self.set_param("Position High", pulse[0])
        await self.set_param("Position Low", pulse[1])
        await self._controller.write_single_register(
                self._motor_id, 
                MotorDefs.TRIGGER,
                0x10)

    # TODO add actual position registers to the motor defs table
    async def get_position(self):
        # TODO: -> read encoder
        pos_l = await self._controller.read_holding_registers(self._motor_id, 0x602D) 
        pos_h = await self._controller.read_holding_registers(self._motor_id, 0x602C)
        pos = (pos_h << 16) & pos_l
        if pos:
            print(f"[Stepper] Motor {self._motor_id} -> Position: {pos}")
            return pos
        return None

    # TODO set param
    async def set_param(self, register, value):
         return await self._controller.write_single_register(self._motor_id, 
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

        while True:
            status = await self.get_param("Motion status") & (1<<5)

            if status is None:
                # print(f"[{self.motor_id}] Failed to read motion status.")
                return MotorStatus.ERROR

            # print(f"[{self.motor_id}] Motion status: {bin(status)}")

            if status:
                # print(f"[{self.motor_id}] Move complete.")
                return MotorStatus.POSITION_REACHED

            if time.monotonic() - start_time > timeout:
                # print(f"[{self.motor_id}] Timeout waiting for move.")
                return MotorStatus.ERROR

            await asyncio.sleep(0.05)

    # async def home(self):
    #     if self._motor_id == MotorDefs.GRIPPER:
    #         print(f"[Motor {self._motor_id} is servo")
    #         return None
    #     pass

        # stop
    async def stop(self):
        #todo 0x40 on trigger to stop
        pass


