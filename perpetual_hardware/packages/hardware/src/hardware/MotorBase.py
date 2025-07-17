import MotorDefs
from ModbusController import SharedModbusController
import time

# 5 steppers/1 servo
# all motors share a modbus object that maintains the connection


motor_stage     = {"motor_id":1, "Ip": 0x0A, "rpm":0xF0, "nrev": 0x2600, "dir": 0, "mpr":50} # todo make the conversion to mm/rev
motor_gripper   = {"motor_id":3, "Ip": 0x0A, "rpm":0x1F0, "nrev": 0xF000, "dir": 1, "mpr":19.992}
motor_rotate    = {"motor_id":4, "Ip": 0x32, "rpm":0x40, "nrev": 0x5800, "dir": 0, "mpr":36} # deg, not mm
motor_x         = {"motor_id":5, "Ip": 0x32, "rpm":0x80, "nrev": 0x8000, "dir": 0, "mpr":80}
# motor_y         = {"motor_id":0x05, "Ip": 0x0A, "rpm":0x40, "p_lim": 0x2600}
# GRIPPER         = 0x06

class Motor():

    def __init__(self, controller: SharedModbusController, params: dict):
            self.motor_id = int(params["motor_id"])        # slave id
            self.controller = controller                   # modbus controller
            self.params = params 

    def configure(self):
         # Peak current
        self.controller.write_single_register(self.motor_id, 
                                                     MotorDefs.get_register_by_name("Peak Current"), 
                                                     int(self.params["Ip"]))
         # Speed in RPM
        self.controller.write_single_register(self.motor_id, 
                                                     MotorDefs.get_register_by_name("Velocity"),
                                                     int(self.params["rpm"]))
         
        self.controller.write_single_register(self.motor_id,
                                                     MotorDefs.get_register_by_name("Motor direction"),
                                                    int(self.params["dir"]))

        return

    # this sort of assumes we're only using path 0, and will have to be adapted if we do anything fancy.
    async def move_to_position(self, value: int): # mm
        if value > self.params["nrev"]:
             print(f"[Stepper] Motor {self.motor_id} -> Cannot Move to position {value}")
             return
        print(f"[Stepper] Motor {self.motor_id} -> Move to position {value}")
        # TODO move the function calls to get/set param
        await self.controller.write_single_register(self.motor_id,
                                                    MotorDefs.get_register_by_name("Motion of Path 0"), 
                                                    0x41) # position mode
        pulse = MotorDefs.rev_to_pulse(MotorDefs.mm_to_rev(value,self.params["mpr"])) # tuple
        await self.controller.write_single_register(self.motor_id, 
                                                    MotorDefs.get_register_by_name("Position High"),
                                                    pulse[0])
        await self.controller.write_single_register(self.motor_id, 
                                                    MotorDefs.get_register_by_name("Position Low"),
                                                    pulse[1])
        await self.controller.write_single_register(self.motor_id, 
                                                    MotorDefs.TRIGGER, 
                                                    0x10) # execute
        
        # wait for move (motion status bit 5)
        while self.controller.read_holding_registers(
            self.motor_id,
            MotorDefs.get_register_by_name("Motion status") & (1 << 5)
            ) == 0:
            
            time.sleep(0.05)

        return 

    # async def get_position(self):
    #     # TODO: -> read encoder
    #     pos = await self.controller.read_holding_registers(self.motor_id, 0x6202, 1)
    #     if pos:
    #         print(f"[Stepper] Motor {self.motor_id} -> Position: {pos[0]}")
    #         return pos[0]
    #     return None

    async def set_param(self, address, value):
         return await self.controller.write_single_register(self.motor_id, address, value)
    
    async def get_param(self, address):
        val = await self.controller.read_holding_registers(self.motor_id, address)
        if val:
            print(f"[Stepper] Motor {self.motor_id} -> val: {val[0]}")
            return val[0]
        return None 
    
    async def home(self):
        if self.motor_id == MotorDefs.GRIPPER:
            print(f"[Motor {self.motor_id} is servo")
            return None
        pass

        # stop
    async def stop(self):
        pass


