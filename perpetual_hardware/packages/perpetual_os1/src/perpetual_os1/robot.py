from perpetual_os1.common import AxisCoordinate, ToolCoordinate
from hardware.ModbusController import SharedModbusController as Controller
from hardware.MotorBase import Motor, Servo, MotorStatus
from hardware.camera import Camera
import commands
import cv2
import asyncio
import robot_state

class Robot:
    def __init__(self, port: str) -> None:
        self._bus = Controller(port=port)
        self._state = robot_state.load_state("config.json")
        self._mx = Motor(
            self._bus, 
            motor_id=self._state["motors"]["x_axis"]["motor_id"], 
        )
        self._my = Motor(
            self._bus, 
            motor_id=self._state["motors"]["y_axis"]["motor_id"], 
        )
        self._mg = Motor(
            self._bus, 
            motor_id=self._state["motors"]["z_axis"]["motor_id"], 
        )
        self._ms = Motor(
            self._bus, 
            motor_id=self._state["motors"]["stage"]["motor_id"], 
        )
        self._mr = Motor(
            self._bus, 
            motor_id=self._state["motors"]["rotate"]["motor_id"], 
        )
        self._md = Motor(
            self._bus, 
            motor_id=self._state["motors"]["door"]["motor_id"], 
        )

        self._gs = Servo(self._bus)
        self._cam = Camera()
        self._grid = self._state["grid"]
        self._cur_item: str = None # some kind of unique identifier

    async def configure(self):
        await self._bus.connect()
        if not self._bus.client.connected:
            print("No Connection to Modbus")
            return
        await self._mx.configure()
        await self._mg.configure()
        await self._mr.configure()
        await self._ms.configure()
        await self._my.configure()
        await self._gs.configure()

    async def retrieve_item(self, label: str):
        pass

    async def place_item(self):
        pass

    async def pick(self, coordinate: AxisCoordinate) -> None:
        await self._gs.safe_open()
        await self._mx.move_config()
        await self._my.move_config()
        await self._mx.move_to_position_no_wait(coordinate.x)
        await self._my.move_to_position_no_wait(coordinate.y)
        await self.gantry_wait()
        #empty move
        await self._ms.move_to_position_no_wait(self._state["robot"]["SLIDE_EXTENSION"]) # 50 max
        await self._mg.move_to_position_no_wait(self._state["robot"]["GRIPPER_EXTENSION_PICK"])
        await self.tool_wait()

        await self._gs.open()
        await self._mg.retract()
        await self._ms.retract()

    async def place(self, coordinate: AxisCoordinate):
        await self._mx.move_config()
        await self._my.move_config()
        await self._mx.move_to_position_no_wait(coordinate.x)
        await self._my.move_to_position_no_wait(coordinate.y)
        await self._ms.move_to_position(self._state["robot"]["SLIDE_EXTENSION"]) # 50 max
        await self._mg.move_to_position(self._state["robot"]["GRIPPER_EXTENSION_PLACE"])
        await self._gs.close()

        await self._ms.move_config()
        await self._mg.move_config()
        await self._mg.move_to_position_no_wait(0)
        await self._ms.move_to_position_no_wait(0)
        await self.tool_wait()

        await self._mg.retract()
        await self._ms.retract()

    async def move(self, coordinate: AxisCoordinate) -> None:
        await self._mx.move_config()
        await self._my.move_config()
        await self._mx.move_to_position_no_wait(coordinate.x)
        await self._my.move_to_position_no_wait(coordinate.y)
        await self.gantry_wait()

    async def gantry_wait(self):
        while True:
            mx_finish = await self._mx.wait_for_move()
            my_finish = await self._my.wait_for_move()

            if mx_finish == MotorStatus.STOPPED and my_finish == MotorStatus.STOPPED:
                break

            await asyncio.sleep(0.01)  # Wait a bit before checking again

    async def tool_wait(self):
        while True:
            mg_finish = await self._mg.wait_for_move()
            ms_finish = await self._ms.wait_for_move()

            if ms_finish == MotorStatus.STOPPED and mg_finish == MotorStatus.STOPPED:
                break

            await asyncio.sleep(0.01)  # Wait a bit before checking again

    # todo, 2D and update grid
    async def get_grid(self):
        pos = 0
        end = 0
        while True: 
            pos = await self.auto_cal()
            if pos:
                self._bays.append(pos)
                await asyncio.sleep(0.01)
                await self._mx.jog(self._state["robot"]["JOG"])
            await asyncio.sleep(0.01)

            if end > 600:
                # Todo set real axis limit
                # todo move up one shelf and repeat
                break

            end = await self._mx.get_position()
            end = self._mx._params["mpr"]*end/self._state["robot"]["PPR"]
    

    def get_storage(self):
        return self._grid

    # def set_storage(self, value: StorageGrid) -> None:
    #     self._storage = value

    # async def idle(self) -> None:
    #     await self.move(self._configuration.idle_position) # basically anywhere along the bottom of the gantry

    async def shutdown(self) -> None:
        if self._bus:
            await self._bus.close()

    #############################

    async def home(self) -> None:
        await self._ms.home()
        await self._mg.home()
        await self._mr.home()
        await self._mx.home()
        await self._my.home()

    async def stop(self) -> None:
        await self._mx.stop()
        await self._mg.stop()
        await self._mr.stop()
        await self._ms.stop()
        await self._my.stop()

    def get_pos(self):
        # get each motor position. return tuple of axis and tool coordinate?
        x = self._mx.get_position()
        y = self._my.get_position()
        g = self._mg.get_position()
        s = self._ms.get_position()
        r = self._mr.get_position()
        return (AxisCoordinate(x, y), ToolCoordinate(g,s,r))
    
    async def centering(self, frame):
        frame = cv2.rotate(frame, cv2.ROTATE_180)
        height, width = frame.shape[:2]
        third = width // 3
        third_h = height // 3

        frame_middle = frame[:, third:2 * third]
        frame_middle = frame_middle[2 * third_h:, :]

        w_e = self._state["motors"]["robot"]["W_EXPECTED"], 
        h_e = self._state["motors"]["robot"]["H_EXPECTED"]

        bbox_crop = self._cam.get_fiducial(frame_middle,
                                           w_e * h_e,
                                           w_e // h_e,
                                           self._state["robot"]["TOLERANCE_AREA"],
                                           self._state["robot"]["TOLERANCE_ASPECT"])

        if bbox_crop is None:
            # print("No white rectangular fiducial detected.")
            return False
        else:
            x_crop, y, w, h = bbox_crop
            x = x_crop + third  # Shift X to full-frame coordinates
            _ , obj_center, frame_center = self._cam.get_center(frame_middle, bbox_crop)
            # print(f"Centered score (0 = perfect): {score:.3f}")
            # print(f"Object center: {obj_center}")
            # print(f"Frame center: {frame_center}")

            # Draw bounding box and centers
            x, y, w, h = bbox_crop
            cv2.rectangle(frame_middle, (x, y), (x + w, y + h), (0, 255, 0), 2)
            cv2.circle(frame_middle, (int(obj_center[0]), int(obj_center[1])), 5, (255, 0, 0), -1)
            cv2.circle(frame_middle, (int(frame_center[0]), int(frame_center[1])), 5, (0, 0, 255), -1)
            offset_mm = (obj_center[0]-frame_center[0])*self._state["robot"]["PXMM"]

            return offset_mm

    async def auto_cal(self):
        frame = await self._cam.capture()
        offset = await self.centering(frame)
        if offset is False:
            await self._mx.jog(self._state["robot"]["JOG"])
            return offset
        else:
            await self._mx.jog(-offset) # camera is upside down
            pos = await self._mx.get_position()
            pos = ((self._mx._params["mpr"]*pos)/self._state["robot"]["PPR"]) #in mm
            return pos

    async def handle_command(self, command: commands.RobotCommands) -> RobotResponse:
        match command:
            case commands.StopCommand():
                await self.stop()
                return commands.RobotCommandSuccessful()
            case commands.InitializeCommand():
                await self.configure()
                return commands.RobotCommandSuccessful()
            case commands.HomeCommand():
                await self.home()
                return commands.RobotCommandSuccessful()
            case commands.MoveCommand(coordinate=coord):
                await self.move(coord)
            # case IdleCommand():
            #     await self.idle()
            #     return RobotCommandSuccessful()
            case _:
                return commands.RobotCommandError(error="Invalid Robot Command")
