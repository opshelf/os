import hardware.MotorDefs as MotorDefs
from hardware.ModbusController import SharedModbusController
import time
import asyncio
from enum import Enum

class MotorStatus(Enum):
    IDLE = 0
    MOVING = 1
    STOPPED = 2
    ERROR = 3

class Servo():
    def __init__(self, controller: SharedModbusController):
            self._motor_id = 0x9                            # slave id
            self._controller = controller
            self._control_reg = 0x3E8
            self._status_reg = 0x07D0                        # status register

    async def configure(self):
        await self._controller.write_single_register(self._motor_id, self._control_reg, 0x0) #clear
        await self._controller.write_single_register(self._motor_id, self._control_reg, 0x1) #enable # do i need this
        await self.wait_for_activate()
        #open
        await self._controller.write_single_register(self._motor_id, 0x03EB, 0x8020) # 0x80|0x20
        await self._controller.write_single_register(self._motor_id, 0x03EC, 0x80)
        #close
        await self._controller.write_single_register(self._motor_id, 0x03ED, 0x80FE) # 0x80|0xFE
        await self._controller.write_single_register(self._motor_id, 0x03EE, 0x80)
        #safe open
        await self._controller.write_single_register(self._motor_id, 0x03EF, 0x80C0) # 0x80|0xFF
        await self._controller.write_single_register(self._motor_id, 0x03F0, 0x80)

    async def wait_for_activate(self):
        start_time = time.monotonic()

        while True:
            status = await self._controller.read_holding_registers(self._motor_id, self._status_reg) & (1<<5)
            if status is None:
                # print("Failed to read activation status.")
                return MotorStatus.ERROR

            if status:
                # print(f"[gripper] Activation complete.")
                return 

            if time.monotonic() - start_time > 3:
                # print(f"[gripper] Timeout waiting for activation.")
                return MotorStatus.ERROR

            await asyncio.sleep(0.01)

    async def open(self):
        await self._controller.write_single_register(self._motor_id, self._control_reg, 0x080B) 
        await self.wait_for_servo()

    async def safe_open(self):
        await self._controller.write_single_register(self._motor_id, self._control_reg, 0x0A0B) 
        await self.wait_for_servo()

    async def close(self):
        await self._controller.write_single_register(self._motor_id, self._control_reg, 0x090B) 
        await self.wait_for_servo()

    async def wait_for_servo(self, timeout: float = 10.0) -> MotorStatus:
        start_time = time.monotonic()

        while True:
            status = await self._controller.read_holding_registers(self._motor_id, self._status_reg)
            status = (status & (1<<6)) or (status & (1<<7))
            if status is None:
                # print("Failed to read gripper status.")
                return MotorStatus.ERROR

            if status:
                # print(f"[gripper] Move complete.")
                return 

            if time.monotonic() - start_time > timeout:
                # print(f"[gripper] Timeout waiting for move.")
                return MotorStatus.ERROR

            await asyncio.sleep(0.01)
         
# stepper
class Motor():
    def __init__(self, controller: SharedModbusController, params: dict):
            self._motor_id = int(params["motor_id"])        # slave id
            self._controller = controller                   # modbus controller
            self._params = params
            self._cur_pos = None
            self._stall_timeout = 0.1                      #second??? 
            self._status: MotorStatus = MotorStatus.IDLE

    async def configure(self):
        # print(self._params["motor_name"])
        # await self.set_param("SI2 input function", 0x26) # neg switch
        await self.set_param("SO2 output function", 0xA1)
        await self._controller.write_single_register(self._motor_id, 0x0167, 100)
        await self._controller.write_single_register(self._motor_id, 0x0169, 100)
        await self._controller.write_single_register(self._motor_id, 0x016B, 10)
        await self.set_param("Allowed max position following error", 4000)
        await self.set_param("Position loop Kp", 50)
        await self.set_param("Max stop time", 100)
        await self.set_param("Acceleration", self._params['accel'])
        await self.set_param("Deceleration", self._params['accel'])
        await self.set_param("Peak current", self._params["Ip"])
        await self.set_param("Motor direction", self._params["dir"])
        await self.set_param("Velocity", self._params["rpm"])
        await self.move_config()
        return

    # async def extend(self):
    #     pass

    # async def retract(self):



    # TODO add axis limits
    async def move_to_position(self, value: float): # mm

        if value > self._params["lim"]:
            raise ValueError(f"Value {value} exceeds motor limit {self._params['lim']} mm")

        pulse = MotorDefs.rev_to_pulse(MotorDefs.mm_to_rev(value,self._params["mpr"])) # tuple
        # await self.set_param("Control word", 0x1111) # clear alarm if any
        # await self.set_param("Control word", 0x1122) # clear alarm if any
        # await self.set_param("Motion of Path 0", 0x1) # abs pos
        # await self.set_param("Motor direction", self._params["dir"])
        # await self.set_param("Velocity", self._params["rpm"])
        await self.set_param("Position High", pulse[0])
        await self.set_param("Position Low", pulse[1])
        await self._controller.write_single_register(
                self._motor_id, 
                MotorDefs.TRIGGER,
                0x10) # start path 0
        
        self._status = MotorStatus.MOVING
        await self.wait_for_move(timeout=10)
        return
    
    # assumes that the motor is already configured
    async def move_to_position_no_wait(self, value: float): # mm
        if value > self._params["lim"]:
            raise ValueError(f"Value {value} exceeds motor limit {self._params['lim']} mm")
        pulse = MotorDefs.rev_to_pulse(MotorDefs.mm_to_rev(value,self._params["mpr"])) # tuple
        # await self.set_param("Control word", 0x1111) # clear alarm if any
        # await self.set_param("Control word", 0x1122) # clear alarm if any
        # await self.set_param("Motor direction", self._params["dir"])
        # await self.set_param("Velocity", self._params["rpm"])
        await self.set_param("Position High", pulse[0])
        await self.set_param("Position Low", pulse[1])
        await self._controller.write_single_register(
                self._motor_id, 
                MotorDefs.TRIGGER,
                0x10) # start path 0
        
        self._status = MotorStatus.MOVING
        return
    
    async def move_config(self):
        await self.set_param("Control word", 0x1111) # clear alarm if any
        await self.set_param("Control word", 0x1122) # clear alarm if any
        await self.set_param("Motion of Path 0", 0x1) # abs pos 0x1
        await self.set_param("Motor direction", self._params["dir"])
        await self.set_param("Velocity", self._params["rpm"])
        
    #TODO return val in mm, not pulses
    async def get_position(self) -> float:
        pos_l = await self.get_param("Position Low (Actual)") 
        pos_h = await self.get_param("Position High (Actual)")
        pos = (pos_h << 16) | pos_l
        if pos:
            ppr = await self.get_param("Pulse/revolution")
            pos = (pos/ppr)*self._params["mpr"]
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
    
    #TODO add checks for position following error (crash detection)
    async def wait_for_move(self, timeout: float = 2.5) -> MotorStatus:
        start_time = time.monotonic()
        last_pos = None
        has_moved = False
        last_movement_time = start_time

        while True:
            status = await self.get_param("Motion status")
            complete = status & (1 << 5)
            fault = status & 1

            await self.get_position()
            now = time.monotonic()

            if status is None:
                print(f"[{self._motor_id}] Failed to read motion status.")
                return None
            
            if last_pos is not None:
                # print(f"cur pos: {round(self._cur_pos, 3)}, last pos: {round(last_pos,3)}") 
                if(self._cur_pos != last_pos):
                    last_movement_time = now
                    has_moved = True
                
            elif last_movement_time is None:
                last_movement_time = now  # initialize once
            
            last_pos = self._cur_pos
            
            if has_moved and (now - last_movement_time > self._stall_timeout):
                print(f"[{self._motor_id}] Stopped: No movement detected for {self._stall_timeout} seconds.")
                # print(status)
                await self.stop()
                return MotorStatus.STOPPED

            if complete:
                print(f"[{self._motor_id}] Move complete.")
                # print(status)
                await self.stop()
                return MotorStatus.STOPPED
            
            if fault:
                print(f"[{self._motor_id}] Fault.")
                # print(status)
                await self.stop()
                return MotorStatus.STOPPED
                # if self._params["motor_name"] == "y_axis":
                #     await self.home_y()
                # else:    
                #     await self.home()
                
                # await self.move_to_position(last_pos)

            if time.monotonic() - start_time > timeout:
                # print(f"[{self._motor_id}] Timeout waiting for move.")
                await self.stop()
                return MotorStatus.STOPPED

            await asyncio.sleep(0.01)

    async def wait_for_home(self, timeout: float = 60) -> MotorStatus:
        start_time = time.monotonic()
        last_pos = None
        has_moved = False
        last_movement_time = start_time

        while True:
            status = await self.get_param("Motion status")
            complete = status & (1 << 5)
            fault = status & 1

            await self.get_position()
            now = time.monotonic()

            if status is None:
                print(f"[{self._motor_id}] Failed to read motion status.")
                return None
            
            if last_pos is not None:
                # print(f"cur pos: {round(self._cur_pos, 3)}, last pos: {round(last_pos,3)}") 
                if(self._cur_pos != last_pos):
                    last_movement_time = now
                    has_moved = True
                
            elif last_movement_time is None:
                last_movement_time = now  # initialize once
            
            last_pos = self._cur_pos
            
            if has_moved and (now - last_movement_time > self._stall_timeout):
                # print(f"[{self._motor_id}] Stopped: No movement detected for {self._stall_timeout} seconds.")
                await self.stop()
                return MotorStatus.STOPPED

            if complete:
                # print(f"[{self._motor_id}] Move complete.")
                await self.stop()
                return MotorStatus.STOPPED
            
            if fault:
                # print(f"[{self._motor_id}] Stopped - Alarm.")
                await self.stop()
                return MotorStatus.STOPPED
                # if self._params["motor_name"] == "y_axis":
                #     await self.home_y()
                # else:    
                #     await self.home()
                
                # await self.move_to_position(last_pos)

            if time.monotonic() - start_time > timeout:
                print(f"[{self._motor_id}] Timeout waiting for move.")
                await self.stop()
                return MotorStatus.STOPPED

            await asyncio.sleep(0.01)


    async def home(self):
        # the homing direction register follows an inverted 
        # definition to the motor direction register
        # so this ends up being the opposite direction anyway
        home_dir = self._params["dir"]^1
        await self.set_param("Control word", 0x1111) # clear alarm if any
        await self.set_param("Control word", 0x1122) # clear alarm if any
        # await self.set_param("Peak current", self._params["Ip"])
        await self.set_param("Motion of Path 0", 0x2) # velocity
        # await self.set_param("Homing high velocity", self._params["home_rpm"]) # velocity
        # await self.set_param("Homing low velocity", self._params["home_rpm"]) # velocity
        await self.set_param("Motor direction", home_dir)
        await self.set_param("Velocity", self._params["home_rpm"])
        # await self.set_param("Homing Mode", home_dir)
        await self._controller.write_single_register(
                self._motor_id, 
                MotorDefs.TRIGGER,
                0x10) # start path 0
        
        self._status = MotorStatus.MOVING

        stop = await self.wait_for_home(timeout=60) 
        if stop == MotorStatus.STOPPED:
            await self._controller.write_single_register(
                self._motor_id,
                MotorDefs.TRIGGER,
                0x21) # set zero position
            
            await self.move_config()
        return 

    async def home_y(self):
        if self._params["motor_name"] != "y_axis":
            return
        await self.set_param("Control word", 0x1111) # clear alarm if any
        await self.set_param("Control word", 0x1122) # clear alarm if any
        await self.set_param("Peak current", self._params["Ip"])
        await self.set_param("Motion of Path 0", 0x8) # velocity
        await self.set_param("Homing Mode", 0x0)
        await self._controller.write_single_register(
                self._motor_id, 
                MotorDefs.TRIGGER,
                0x20) # start path 0

        self._status = MotorStatus.MOVING

        stop = await self.wait_for_move(timeout=60) 

        if stop:
            await self._controller.write_single_register(
                self._motor_id,
                MotorDefs.TRIGGER,
                0x21) # set zero position
            
            await self.move_config()

        return 
        
    # lazy retraction method 
    async def retract(self):
        home_dir = self._params["dir"]^1
        await self.set_param("Control word", 0x1111) # clear alarm if any
        await self.set_param("Control word", 0x1122) # clear alarm if any
        await self.set_param("Motion of Path 0", 0x2) # velocity
        await self.set_param("Motor direction", home_dir)
        await self.set_param("Velocity", self._params["ret_rpm"]) # only use with gripper and slide
        await self._controller.write_single_register(
                self._motor_id, 
                MotorDefs.TRIGGER,
                0x10) # start path 0
        
        # # if home successful
        if (await self.wait_for_move(timeout=10) == MotorStatus.STOPPED):
            await self._controller.write_single_register(
                self._motor_id,
                MotorDefs.TRIGGER,
                0x21) # set zero position
            
            await self.move_config()

        return
    
    async def retract_no_wait(self):
        home_dir = self._params["dir"]^1
        await self.set_param("Control word", 0x1111) # clear alarm if any
        await self.set_param("Control word", 0x1122) # clear alarm if any
        await self.set_param("Motion of Path 0", 0x2) # velocity
        await self.set_param("Motor direction", home_dir)
        await self.set_param("Velocity", self._params["ret_rpm"]) # only use with gripper and slide
        await self._controller.write_single_register(
                self._motor_id, 
                MotorDefs.TRIGGER,
                0x10) # start path 0
        
        # # # if home successful
        # if (await self.wait_for_move(timeout=10) == MotorStatus.STOPPED):
        #     await self._controller.write_single_register(
        #         self._motor_id,
        #         MotorDefs.TRIGGER,
        #         0x21) # set zero position
            
        # await self.move_config()

        return
    
    # relative motion
    async def jog(self, dist):
        pulse = MotorDefs.rev_to_pulse(MotorDefs.mm_to_rev(dist,self._params["mpr"])) # tuple
        await self.set_param("Control word", 0x1111) # clear alarm if any
        await self.set_param("Control word", 0x1122) # clear alarm if any
        await self.set_param("Motion of Path 0", 0x41) # rel pos
        await self.set_param("Motor direction", self._params["dir"])
        await self.set_param("Velocity", self._params["rpm"])
        # await self.set_param("Velocity", 32)
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
        await self.move_config()
        return MotorStatus.STOPPED

    async def stop(self):
        await self._controller.write_single_register(
        self._motor_id, 
        MotorDefs.TRIGGER,
        0x40)

        return MotorStatus.STOPPED


