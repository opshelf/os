import perpetual_os1.common as c

from hardware.controller import SharedModbusController as Controller
from hardware.motor import Motor, Servo, MotorStatus, MOTOR_WAIT_TIMEOUT
from hardware.toolcam import ToolCamera
from perpetual_os1.exceptions import (
    MotorError, CameraError, ConfigError,
    ToolError, GripperError, MovementError
)

from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple, Union, Literal, Annotated, List, Any

@dataclass
class FiducialRegistry:
    # store world positions (motor coordinates) to avoid double counting
    items: List[float] = field(default_factory=list)

    def seen(self, yw: float, tol: float) -> bool:
        for yy in self.items:
            if abs(yy - yw) <= tol:
                return True
        return False

    def add(self, yw: float):
        self.items.append(yw)
 
class ToolMotionStatus(Enum):
    IDLE = 0
    MOVING = 1
    ERROR = 2

class ToolItemStatus(Enum):
    EMPTY = 0
    LOADED = 1
    ERROR = 2

@dataclass
class ToolStatus:
    motion: ToolMotionStatus
    item: ToolItemStatus


GANTRY_WAIT_TIMEOUT = MOTOR_WAIT_TIMEOUT * 2
TOOL_WAIT_TIMEOUT = MOTOR_WAIT_TIMEOUT * 2

class Tool:
    def __init__(self, motorcontroller: Controller, params: c.Parameters):
        self._motorcontroller = motorcontroller
        self._istatus = ToolItemStatus.EMPTY
        self._mstatus = ToolMotionStatus.IDLE
        self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
        self._params = params

        self._mx = Motor(self._motorcontroller, self._params.motors.by_name("x_axis"))
        self._my = Motor(self._motorcontroller, self._params.motors.by_name("y_axis"))
        self._mg = Motor(self._motorcontroller, self._params.motors.by_name("z_axis"))
        self._ms = Motor(self._motorcontroller, self._params.motors.by_name("stage"))
        self._mr = Motor(self._motorcontroller, self._params.motors.by_name("rotate"))
        self._gs = Servo(self._motorcontroller)

        self._cam = ToolCamera(self._params.poe_camera)

        # LEDs

## MOTORS
    def _has_cached_camera_calibration(self) -> bool:
        """Return True when calibration data has been persisted in configuration."""
        camera_cfg = self._params.poe_camera
        required = (
            camera_cfg.mtx,
            camera_cfg.dist,
            camera_cfg.H,
            camera_cfg.H_ref_size,
            camera_cfg.pattern_size,
            camera_cfg.board_W_mm,
            camera_cfg.board_H_mm,
            camera_cfg.square_size_mm,
        )
        return (
            getattr(camera_cfg, "calibration_status", None) == "calibrated"
            and all(value is not None for value in required)
        )

    async def configure(self):
        """
        Configure all motors and home the tool.
        
        Raises:
            MotorError: If motor configuration or homing fails
            CameraError: If camera initialization fails
        """
        try:
            await self._gs.configure()
            await self._my.configure()
            await self._mx.configure()
            await self._mg.configure()
            await self._ms.configure()
            await self._mr.configure()
            await self.home()
            if not self._has_cached_camera_calibration():
                self._cam.get_correct()
            self._mstatus = ToolMotionStatus.IDLE
        except Exception as e:
            self._mstatus = ToolMotionStatus.ERROR
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
            raise MotorError(f"Tool configuration failed: {str(e)}") from e

    async def get_param(self, param: str) -> Optional[Any]:
        """Get a parameter value from tool configuration."""
        try:
            if not hasattr(self._params.parameters, param):
                raise KeyError(f"Parameter '{param}' not found")
            return getattr(self._params.parameters, param)
        except Exception as e:
            raise ConfigError(f"Failed to get parameter {param}: {str(e)}") from e

    async def home(self) -> None:
        """
        Home all motors in a specific sequence.
        
        Raises:
            MotorError: If any motor fails to home
        """
        try:
            await self._ms.home()
            await self._mg.home()
            await self._mr.home()
            await self._mx.home()
            await self._my.jog(5)
            await self._my.home()
            self._mstatus = ToolMotionStatus.IDLE
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
        except Exception as e:
            self._mstatus = ToolMotionStatus.ERROR
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
            raise MotorError(f"Failed to home tool: {str(e)}") from e

    async def clear_alarms(self) -> None:
        """
        Clear alarms by homing gripper and stage motors.
        
        Raises:
            MotorError: If homing fails during alarm clear
        """
        try:
            await self._mg.home()
            await self._ms.home()
        except Exception as e:
            self._mstatus = ToolMotionStatus.ERROR
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
            raise MotorError(f"Failed to clear alarms: {str(e)}") from e

    async def stop(self) -> None:
        """
        Stop all motors immediately.
        
        Raises:
            MotorError: If any motor fails to stop
        """
        try:
            await self._mx.stop()
            await self._mg.stop()
            await self._mr.stop()
            await self._ms.stop()
            await self._my.stop()
            self._mstatus = ToolMotionStatus.IDLE
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
        except Exception as e:
            self._mstatus = ToolMotionStatus.ERROR
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
            raise MotorError(f"Failed to stop tool: {str(e)}") from e

    async def move(self, coordinate: c.AxisCoordinate) -> None:
        """
        Move gantry to specified coordinates.
        
        Raises:
            MovementError: If movement fails or times out
        """
        try:
            self._mstatus = ToolMotionStatus.MOVING
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
            await self._my.move_to_position_no_wait(coordinate.y)
            await self._mx.move_to_position_no_wait(coordinate.x)
            await self.gantry_wait()
            self._mstatus = ToolMotionStatus.IDLE
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
        except Exception as e:
            self._mstatus = ToolMotionStatus.ERROR
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
            raise MovementError(f"Gantry movement failed: {str(e)}") from e

    async def retract(self) -> None:
        """
        Retract gripper and slide to home positions.
        
        Raises:
            MovementError: If retraction fails
        """
        try:
            self._mstatus = ToolMotionStatus.MOVING
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
            await self._mg.move_to_position(1)
            await self._ms.move_to_position(1)
            self._mstatus = ToolMotionStatus.IDLE
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
        except Exception as e:
            self._mstatus = ToolMotionStatus.ERROR
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
            raise MovementError(f"Tool retraction failed: {str(e)}") from e

    async def extend(self) -> None:
        """
        Extend gripper and slide to operation positions.
        
        Raises:
            MovementError: If extension fails
            ConfigError: If extension parameters are missing
        """
        try:
            slide_ext = await self.get_param("SLIDE_EXTENSION")
            grip_ext = await self.get_param("GRIPPER_EXTENSION_PLACE")
            if slide_ext is None or grip_ext is None:
                raise ConfigError("Missing extension parameters")
            
            self._mstatus = ToolMotionStatus.MOVING
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
            await self._ms.move_to_position(slide_ext)
            await self._mg.move_to_position(grip_ext)
            self._mstatus = ToolMotionStatus.IDLE
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
        except Exception as e:
            self._mstatus = ToolMotionStatus.ERROR
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
            if isinstance(e, ConfigError):
                raise
            raise MovementError(f"Tool extension failed: {str(e)}") from e
        
    async def extend_IO(self) -> None:
        """
        Extend gripper and slide to operation positions.
        
        Raises:
            MovementError: If extension fails
            ConfigError: If extension parameters are missing
        """
        try:
            io_offset = -3
            slide_ext = await self.get_param("SLIDE_EXTENSION")
            grip_ext = await self.get_param("GRIPPER_EXTENSION_PLACE")
            if slide_ext is None or grip_ext is None:
                raise ConfigError("Missing extension parameters")
            
            self._mstatus = ToolMotionStatus.MOVING
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
            await self._ms.move_to_position(slide_ext+io_offset)
            await self._mg.move_to_position(grip_ext)
            self._mstatus = ToolMotionStatus.IDLE
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
        except Exception as e:
            self._mstatus = ToolMotionStatus.ERROR
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
            if isinstance(e, ConfigError):
                raise
            raise MovementError(f"Tool extension failed: {str(e)}") from e

    async def extend_empty(self) -> None:
        """
        Extend empty tool without waiting between moves.
        
        Raises:
            MovementError: If extension fails
            ConfigError: If extension parameters are missing
        """
        try:
            slide_ext = await self.get_param("SLIDE_EXTENSION")
            grip_ext = await self.get_param("GRIPPER_EXTENSION_PICK")
            if slide_ext is None or grip_ext is None:
                raise ConfigError("Missing extension parameters")
            
            self._mstatus = ToolMotionStatus.MOVING
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
            await self._ms.move_to_position_no_wait(slide_ext)
            await self._mg.move_to_position_no_wait(grip_ext)
            await self.tool_wait(homing=False)
            self._mstatus = ToolMotionStatus.IDLE
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
        except Exception as e:
            self._mstatus = ToolMotionStatus.ERROR
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
            if isinstance(e, ConfigError):
                raise
            raise MovementError(f"Tool empty extension failed: {str(e)}") from e
    
    async def retract_empty(self) -> None:
        """
        Retract empty tool without waiting between moves.
        
        Raises:
            MovementError: If retraction fails
        """
        try:
            self._mstatus = ToolMotionStatus.MOVING
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
            await self._ms.move_to_position_no_wait(1)
            await self._mg.move_to_position_no_wait(1)
            await self.tool_wait(homing=True)
            self._mstatus = ToolMotionStatus.IDLE
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
        except Exception as e:
            self._mstatus = ToolMotionStatus.ERROR
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
            raise MovementError(f"Tool empty retraction failed: {str(e)}") from e
        
    async def pick(self, coordinate: c.AxisCoordinate) -> None:
        """
        Execute pick sequence at specified coordinates.
        
        Raises:
            MovementError: If any movement fails
            GripperError: If gripper operation fails
        """
        try:
            self._mstatus = ToolMotionStatus.MOVING
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
            
            front_angle = await self.get_param("FRONT")
            back_angle = await self.get_param("BACK")
            target_angle = front_angle if coordinate.s == 0 else back_angle

            await self._mr.move_to_position(target_angle)
            await self._gs.safe_open()
            await self.move(coordinate)
            await self.extend_empty()
            await self._gs.open()
            await self.retract()
            
            self._istatus = ToolItemStatus.LOADED
            self._mstatus = ToolMotionStatus.IDLE
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
        except Exception as e: # add rehoming???
            self._mstatus = ToolMotionStatus.ERROR
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
            if isinstance(e, MovementError):
                raise
            raise GripperError(f"Pick operation failed: {str(e)}") from e

    async def place(self, coordinate: c.AxisCoordinate):
        """
        Execute place sequence at specified coordinates.
        
        Raises:
            MovementError: If any movement fails
            GripperError: If gripper operation fails
        """
        try:
            self._mstatus = ToolMotionStatus.MOVING
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
            
            front_angle = await self.get_param("FRONT")
            back_angle = await self.get_param("BACK")
            target_angle = front_angle if coordinate.s == 0 else back_angle

            await self._mr.move_to_position(target_angle)
            await self.move(coordinate)
            await self.extend()
            await self._gs.close()
            await self.retract_empty()
            
            self._istatus = ToolItemStatus.EMPTY
            self._mstatus = ToolMotionStatus.IDLE
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
        except Exception as e:
            self._mstatus = ToolMotionStatus.ERROR
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
            if isinstance(e, MovementError):
                raise
            raise GripperError(f"Place operation failed: {str(e)}") from e

    # todo "homeable position checking"
    async def gantry_wait(self):
        """
        Wait for gantry motors to complete movement.
        
        Raises:
            MovementError: If wait times out or motors error
        """
        try:
            terminal_error_states = {MotorStatus.ERROR, MotorStatus.FAULT, MotorStatus.TIMEOUT}
            while True:
                mx_f = await self._mx.wait_for_move(timeout=GANTRY_WAIT_TIMEOUT)
                my_f = await self._my.wait_for_move(timeout=GANTRY_WAIT_TIMEOUT)
                if mx_f == MotorStatus.STOPPED and my_f == MotorStatus.STOPPED:
                    return
                if mx_f in terminal_error_states or my_f in terminal_error_states:
                    raise MovementError("Gantry motor error during wait")
        except Exception as e:
            self._mstatus = ToolMotionStatus.ERROR
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
            raise MovementError(f"Gantry wait failed: {str(e)}") from e

    async def tool_wait(self, homing):
        """
        Wait for tool motors to complete movement.
        
        Raises:
            MovementError: If wait times out or motors error
        """
        try:
            terminal_error_states = {MotorStatus.ERROR, MotorStatus.FAULT, MotorStatus.TIMEOUT}
            while True:
                mg_f = await self._mg.wait_for_move(timeout=TOOL_WAIT_TIMEOUT, homing=homing)
                ms_f = await self._ms.wait_for_move(timeout=TOOL_WAIT_TIMEOUT, homing=homing)
                if mg_f == MotorStatus.STOPPED and ms_f == MotorStatus.STOPPED:
                    return
                if mg_f in terminal_error_states or ms_f in terminal_error_states:
                    raise MovementError("Tool motor error during wait")
        except Exception as e:
            self._mstatus = ToolMotionStatus.ERROR
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
            raise MovementError(f"Tool wait failed: {str(e)}") from e
            
    async def get_pos(self):
        # get each motor position. return tuple of axis and tool coordinate?
        x = await self._mx.get_position()
        y = await self._my.get_position()
        g = await self._mg.get_position()
        s = await self._ms.get_position()
        r = await self._mr.get_position()
        return (c.AxisCoordinate(x, y), c.ToolCoordinate(g,s,r))
    
    ### CAMERA

    async def get_shelves(self):
        await self._mr.move_to_position(0.5)
        shelves = []
        reg = FiducialRegistry()
        s = await self.get_param("SCAN_POSITION")
        await self.move(c.AxisCoordinate(s[0], s[1]))
        
        while True:
            frame = await self._cam.capture() # TODO error handling
            frame = self._cam.correct(frame)
            rect = self._cam.detect_white_squares(frame)

            # correction TODO clean up
            if rect is not None:
                _, dy = self._cam.get_center(frame, rect)
                if dy > 0:
                    await self._my.jog(dy * self.get_param("PYMM")) # 30 (figure out bottom shelf)
                    frame = await self._cam.capture()
                    frame = self._cam.correct(frame)
                    rect = self._cam.detect_white_squares(frame)
                    if rect is not None:
                        _, dy = self._cam.get_center(frame, rect)
                        if dy is not None:
                            await self._my.jog(dy * self.get_param("PYMM"))
                            yw = round(await self._my.get_position(), 4)
                            if not reg.seen(yw, 20):
                                reg.add(yw)
                                shelves.append(yw)                    

            end_y = await self._my.get_position()
            lim = self._params.motors.by_name("y_axis").lim 
            if end_y + self.get_param("JOG_Y") > lim:
                break
            await self._my.jog(self.get_param("JOG_Y"))
        return shelves

    async def update_grid(self, shelves: List[float]):
        anchors = []
        await self._mr.move_to_position(0.5)
        s = await self.get_param("SCAN_POSITION")
        await self.move(c.AxisCoordinate(s[0], s[1]))
        
        for s in shelves:
            await self._my.move_to_position(s)
            frame = await self._cam.capture() # TODO error handling
            frame = self._cam.correct(frame)
            _, width = frame.shape[:2]
            third = width // 3
            frame = frame[:, third:2 * third]
            rect = self._cam.detect_white_squares(frame)
            if rect is not None:
                dx, _ = self._cam.get_center(frame, rect)
                if dx is not None:
                    await self._mx.jog(dx * self.get_param("PXMM")) # 30 (figure out bottom shelf)
                    frame = await self._cam.capture()
                    frame = self._cam.correct(frame)
                    _, width = frame.shape[:2]
                    third = width // 3
                    frame = frame[:, third:2 * third]
                    rect = self._cam.detect_white_squares(frame)
                    if rect is not None:
                        dx, _ = self._cam.get_center(frame, rect)
                        if dx is not None:
                            await self._mx.jog(dx * self.get_param("PXMM"))
                            xw = round(await self._mx.get_position(), 4)
                            anchors.append(xw)                    
        return anchors
    
    async def auto_cal(self):
        """Get shelf positions and anchor points for calibration."""
        shelves = await self.get_shelves()
        anchors = await self.update_grid(shelves)
        return shelves, anchors
        # todo eventually make this auto generate the slope of each shelf by having two scan positions in x
