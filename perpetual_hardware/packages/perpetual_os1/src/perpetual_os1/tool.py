import perpetual_os1.common as c

from hardware.controller import SharedModbusController as Controller
from hardware.motor import Motor, Servo, MotorStatus, MOTOR_WAIT_TIMEOUT
from hardware.toolcam import ToolCamera
from perpetual_os1.exceptions import (
    MotorError, ConfigError, CameraError,
    GripperError, MovementError,
    ScanTimeoutError, MotorWaitTimeoutError
)

import logging
import time
import cv2
import sentry_sdk

from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Literal, List, Any

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
SHELF_SCAN_TIMEOUT = 300.0
GRID_UPDATE_TIMEOUT_PER_SHELF = 60.0
GANTRY_OUTER_TIMEOUT = 120.0
TOOL_OUTER_TIMEOUT = 60.0

class Tool:
    def __init__(self, motorcontroller: Controller, params: c.Parameters, safety=None):
        self._motorcontroller = motorcontroller
        self._istatus = ToolItemStatus.EMPTY
        self._mstatus = ToolMotionStatus.IDLE
        self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
        self._params = params
        self._fault_recovery_active = False
        self._fault_recovery_attempted = False
        self._guard_paused = False
        self._logger = logging.getLogger(__name__)

        self._mx = Motor(self._motorcontroller, self._params.motors.by_name("x_axis"), safety)
        self._my = Motor(self._motorcontroller, self._params.motors.by_name("y_axis"), safety)
        self._mg = Motor(self._motorcontroller, self._params.motors.by_name("z_axis"), safety)
        self._ms = Motor(self._motorcontroller, self._params.motors.by_name("stage"), safety)
        self._mr = Motor(self._motorcontroller, self._params.motors.by_name("rotate"), safety)
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
            self._fault_recovery_attempted = False
            await self._gs.configure()
            await self._my.configure()
            await self._mx.configure()
            await self._mg.configure()
            await self._ms.configure()
            await self._mr.configure()
            await self.home()
            # if not self._has_cached_camera_calibration():
            # self._cam.get_correct()
            self._mstatus = ToolMotionStatus.IDLE
        except Exception as e:
            await self._handle_fault(e)
            raise MotorError(f"Tool configuration failed: {str(e)}") from e

    async def _handle_fault(self, exc: Exception) -> None:
        """Mark the tool as faulted and attempt recovery."""
        self._mstatus = ToolMotionStatus.ERROR
        self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
        if self._fault_recovery_active or isinstance(exc, ConfigError):
            return
        await self._recover_from_fault()

    async def _recover_from_fault(self) -> None:
        """Move to idle and re-home after a fault, logging secondary failures."""
        if self._fault_recovery_active or self._fault_recovery_attempted:
            return

        self._fault_recovery_active = True
        recovery_errors: List[str] = []

        try:
            idle_coord: Optional[c.AxisCoordinate] = None
            dump_coord: Optional[c.AxisCoordinate] = None

            try:
                idle_position = await self.get_param("IDLE_POSITION")
                if idle_position is not None:
                    ix, iy = idle_position
                    idle_coord = c.AxisCoordinate(ix, iy)
            except Exception as e:
                recovery_errors.append(f"IDLE_POSITION lookup: {e}")
                idle_coord = None

            try:
                dump_position = await self.get_param("FAULT_DUMP_POSITION")
                if dump_position is not None:
                    dx, dy = dump_position
                    dump_coord = c.AxisCoordinate(dx, dy, 1)
            except Exception as e:
                recovery_errors.append(f"FAULT_DUMP_POSITION lookup: {e}")
                dump_coord = None

            if idle_coord is not None:
                try:
                    await self.retract()
                except Exception as e:
                    recovery_errors.append(f"retract: {e}")

                try:
                    await self.move(idle_coord)
                except Exception as e:
                    recovery_errors.append(f"move to idle: {e}")

            try:
                await self.home()
            except Exception as e:
                recovery_errors.append(f"home: {e}")

            if dump_coord is not None and idle_coord is not None:
                try:
                    await self.place(dump_coord)
                    await self.move(idle_coord)
                except Exception as e:
                    recovery_errors.append(f"dump contents: {e}")

            if recovery_errors:
                self._logger.warning(
                    "Fault recovery completed with %d errors: %s",
                    len(recovery_errors),
                    "; ".join(recovery_errors)
                )
                sentry_sdk.capture_message(
                    f"Fault recovery completed with {len(recovery_errors)} errors",
                    level="warning"
                )
        finally:
            self._fault_recovery_active = False
            self._fault_recovery_attempted = True

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
            await self._handle_fault(e)
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
            await self._handle_fault(e)
            raise MotorError(f"Failed to clear alarms: {str(e)}") from e

    def pause_for_curtain(self) -> None:
        if self._guard_paused:
            return
        self._guard_paused = True
        self._logger.info("Curtain guard pausing tool")

    def resume_after_curtain(self) -> None:
        if not self._guard_paused:
            return
        self._guard_paused = False
        self._logger.info("Curtain guard cleared for tool")

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
            await self._handle_fault(e)
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
            await self._handle_fault(e)
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
            grip_rpm = await self.get_param("GRIPPER_MOVE_RPM")
            grip_accel = await self.get_param("GRIPPER_MOVE_ACCEL")
            if grip_rpm is None or grip_accel is None:
                raise ConfigError("Missing gripper move speed")
            await self._mg.move_to_position(1, rpm=grip_rpm, accel=grip_accel)
            await self._ms.move_to_position(2.5)
            self._mstatus = ToolMotionStatus.IDLE
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
        except Exception as e:
            await self._handle_fault(e)
            raise MovementError(f"Tool retraction failed: {str(e)}") from e

    def _with_gripper_offset(self, value: float, coordinate: Optional[c.AxisCoordinate]) -> float:
        """Apply per-cell z offset to the configured gripper extension."""
        if coordinate is None:
            return value
        return value + getattr(coordinate, "z_offset", 0.0)

    async def _get_slide_extension(self, coordinate: Optional[c.AxisCoordinate]) -> float:
        """Select slide extension for front/back shelves."""
        front_ext = await self.get_param("SLIDE_EXTENSION_FRONT")
        back_ext = await self.get_param("SLIDE_EXTENSION_BACK")
        if front_ext is None or back_ext is None:
            raise ConfigError("Missing slide extension parameters")
        if coordinate is None:
            return front_ext
        return front_ext if coordinate.s == 0 else back_ext

    async def rotate_to_position(self, position: Literal["front", "back"]) -> None:
        """
        Rotate the tool to the configured front or back angle.

        Raises:
            MovementError: If rotation fails
            ConfigError: If rotation parameters are missing
        """
        try:
            param = "FRONT" if position == "front" else "BACK"
            target_angle = await self.get_param(param)
            if target_angle is None:
                raise ConfigError(f"Missing rotation parameter: {param}")

            self._mstatus = ToolMotionStatus.MOVING
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
            await self._mr.move_to_position(target_angle)
            self._mstatus = ToolMotionStatus.IDLE
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
        except Exception as e:
            await self._handle_fault(e)
            if isinstance(e, ConfigError):
                raise
            raise MovementError(f"Tool rotation failed: {str(e)}") from e

    async def extend(self, coordinate: Optional[c.AxisCoordinate] = None) -> None:
        """
        Extend gripper and slide to operation positions.
        
        Raises:
            MovementError: If extension fails
            ConfigError: If extension parameters are missing
        """
        try:
            slide_ext = await self._get_slide_extension(coordinate)
            grip_ext = await self.get_param("GRIPPER_EXTENSION_PLACE")
            grip_rpm = await self.get_param("GRIPPER_MOVE_RPM")
            grip_accel = await self.get_param("GRIPPER_MOVE_ACCEL")
            if slide_ext is None or grip_ext is None or grip_rpm is None or grip_accel is None:
                raise ConfigError("Missing extension parameters")
            grip_ext = self._with_gripper_offset(grip_ext, coordinate)
            
            self._mstatus = ToolMotionStatus.MOVING
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
            await self._ms.move_to_position(slide_ext)
            await self._mg.move_to_position(grip_ext, rpm=grip_rpm, accel=grip_accel)
            self._mstatus = ToolMotionStatus.IDLE
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
        except Exception as e:
            await self._handle_fault(e)
            if isinstance(e, ConfigError):
                raise
            raise MovementError(f"Tool extension failed: {str(e)}") from e
        
    async def extend_IO(self, coordinate: Optional[c.AxisCoordinate] = None) -> None:
        """
        Extend gripper and slide to operation positions.
        
        Raises:
            MovementError: If extension fails
            ConfigError: If extension parameters are missing
        """
        try:
            io_offset = 0
            slide_ext = await self._get_slide_extension(coordinate)
            grip_ext = await self.get_param("GRIPPER_EXTENSION_PLACE")
            grip_rpm = await self.get_param("GRIPPER_MOVE_RPM")
            grip_accel = await self.get_param("GRIPPER_MOVE_ACCEL")
            if slide_ext is None or grip_ext is None or grip_rpm is None or grip_accel is None:
                raise ConfigError("Missing extension parameters")
            grip_ext = self._with_gripper_offset(grip_ext, coordinate)
            
            self._mstatus = ToolMotionStatus.MOVING
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
            await self._ms.move_to_position(slide_ext + io_offset)
            await self._mg.move_to_position(grip_ext, rpm=grip_rpm, accel=grip_accel)
            self._mstatus = ToolMotionStatus.IDLE
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
        except Exception as e:
            await self._handle_fault(e)
            if isinstance(e, ConfigError):
                raise
            raise MovementError(f"Tool extension failed: {str(e)}") from e

    async def extend_empty(self, coordinate: Optional[c.AxisCoordinate] = None) -> None:
        """
        Extend empty tool without waiting between moves.
        
        Raises:
            MovementError: If extension fails
            ConfigError: If extension parameters are missing
        """
        try:
            slide_ext = await self._get_slide_extension(coordinate)
            grip_ext = await self.get_param("GRIPPER_EXTENSION_PICK")
            grip_rpm = await self.get_param("GRIPPER_MOVE_RPM")
            grip_accel = await self.get_param("GRIPPER_MOVE_ACCEL")
            if slide_ext is None or grip_ext is None or grip_rpm is None or grip_accel is None:
                raise ConfigError("Missing extension parameters")
            grip_ext = self._with_gripper_offset(grip_ext, coordinate)
            
            self._mstatus = ToolMotionStatus.MOVING
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
            await self._ms.move_to_position_no_wait(slide_ext)
            await self._mg.move_to_position_no_wait(grip_ext, rpm=grip_rpm, accel=grip_accel)
            await self.tool_wait(homing=False)
            self._mstatus = ToolMotionStatus.IDLE
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
        except Exception as e:
            await self._handle_fault(e)
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
            await self._ms.move_to_position_no_wait(2.5)
            grip_rpm = await self.get_param("GRIPPER_MOVE_RPM")
            grip_accel = await self.get_param("GRIPPER_MOVE_ACCEL")
            if grip_rpm is None or grip_accel is None:
                raise ConfigError("Missing gripper move speed")
            await self._mg.move_to_position_no_wait(1, rpm=grip_rpm, accel=grip_accel)
            await self.tool_wait(homing=True)
            self._mstatus = ToolMotionStatus.IDLE
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
        except Exception as e:
            await self._handle_fault(e)
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
            
            target_position = "front" if coordinate.s == 0 else "back"


            await self._gs.safe_open()
            await self.move(coordinate)
            await self.rotate_to_position(target_position)
            await self.extend_empty(coordinate)
            await self._gs.open()
            await self.retract()
            
            self._istatus = ToolItemStatus.LOADED
            self._mstatus = ToolMotionStatus.IDLE
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
        except Exception as e: # add rehoming???
            await self._handle_fault(e)
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
            
            target_position = "front" if coordinate.s == 0 else "back"

            await self.move(coordinate)
            await self.rotate_to_position(target_position)
            await self.extend(coordinate)
            await self._gs.close()
            await self.retract_empty()
            
            self._istatus = ToolItemStatus.EMPTY
            self._mstatus = ToolMotionStatus.IDLE
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
        except Exception as e:
            await self._handle_fault(e)
            if isinstance(e, MovementError):
                raise
            raise GripperError(f"Place operation failed: {str(e)}") from e

    async def gantry_wait(self, timeout_seconds: float = GANTRY_OUTER_TIMEOUT) -> None:
        """
        Wait for gantry motors to complete movement.

        Raises:
            MotorWaitTimeoutError: If outer timeout exceeded
            MovementError: If motors error during wait
        """
        start_time = time.monotonic()
        terminal_error_states = {MotorStatus.ERROR, MotorStatus.FAULT, MotorStatus.TIMEOUT}

        try:
            while True:
                if time.monotonic() - start_time > timeout_seconds:
                    raise MotorWaitTimeoutError(f"Gantry wait exceeded {timeout_seconds}s timeout")

                mx_f = await self._mx.wait_for_move(timeout=GANTRY_WAIT_TIMEOUT)
                my_f = await self._my.wait_for_move(timeout=GANTRY_WAIT_TIMEOUT)

                if mx_f == MotorStatus.STOPPED and my_f == MotorStatus.STOPPED:
                    return

                if mx_f in terminal_error_states or my_f in terminal_error_states:
                    raise MovementError(f"Gantry motor error during wait: x={mx_f.name}, y={my_f.name}")

        except MotorWaitTimeoutError:
            raise
        except MovementError:
            await self._handle_fault(MovementError("Gantry motor error"))
            raise
        except Exception as e:
            await self._handle_fault(e)
            raise MovementError(f"Gantry wait failed: {str(e)}") from e

    async def tool_wait(self, homing: bool, timeout_seconds: float = TOOL_OUTER_TIMEOUT) -> None:
        """
        Wait for tool motors to complete movement.

        Raises:
            MotorWaitTimeoutError: If outer timeout exceeded
            MovementError: If motors error during wait
        """
        start_time = time.monotonic()
        terminal_error_states = {MotorStatus.ERROR, MotorStatus.FAULT, MotorStatus.TIMEOUT}

        try:
            while True:
                if time.monotonic() - start_time > timeout_seconds:
                    raise MotorWaitTimeoutError(f"Tool wait exceeded {timeout_seconds}s timeout")

                mg_f = await self._mg.wait_for_move(timeout=TOOL_WAIT_TIMEOUT, homing=homing)
                ms_f = await self._ms.wait_for_move(timeout=TOOL_WAIT_TIMEOUT, homing=homing)

                if mg_f == MotorStatus.STOPPED and ms_f == MotorStatus.STOPPED:
                    return

                if mg_f in terminal_error_states or ms_f in terminal_error_states:
                    raise MovementError(f"Tool motor error during wait: gripper={mg_f.name}, stage={ms_f.name}")

        except MotorWaitTimeoutError:
            raise
        except MovementError:
            await self._handle_fault(MovementError("Tool motor error"))
            raise
        except Exception as e:
            await self._handle_fault(e)
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

    async def get_shelves(self, timeout_seconds: float = SHELF_SCAN_TIMEOUT) -> List[float]:
        start_time = time.monotonic()
        shelves: List[float] = []

        try:
            await self.home()
            await self._mx.move_to_position(100)
            await self.rotate_to_position("back")
            reg = FiducialRegistry()
            s = await self.get_param("SCAN_POSITION")
            await self.move(c.AxisCoordinate(s[0]-25, s[1]))

            while True:
                if time.monotonic() - start_time > timeout_seconds:
                    raise ScanTimeoutError(
                        f"Shelf scan exceeded {timeout_seconds}s timeout, found {len(shelves)} shelves"
                    )

                try:
                    frame = await self._cam.capture()
                except Exception as e:
                    raise CameraError(f"Camera capture failed during shelf scan: {e}") from e

                if frame is None:
                    raise CameraError("Camera returned empty frame during shelf scan")

                try:
                    frame = self._cam.correct(frame)
                except Exception as e:
                    raise CameraError(f"Frame correction failed: {e}") from e

                cv2.imwrite("frame_correct.png", frame)
                rect = self._cam.detect_white_squares(frame)

                if rect is not None:
                    _, dy = self._cam.get_center(frame, rect)
                    if dy > 0:
                        pymm = await self.get_param("PYMM")
                        await self._my.jog(dy/pymm)

                        frame = await self._cam.capture()
                        if frame is None:
                            raise CameraError("Camera returned empty frame during correction")

                        frame = self._cam.correct(frame)
                        rect = self._cam.detect_white_squares(frame)
                        if rect is not None:
                            _, dy = self._cam.get_center(frame, rect)
                            if dy is not None:
                                await self._my.jog(dy/pymm)
                                posy = await self._my.get_position()
                                yw = round(posy+26, 4)
                                if not reg.seen(yw, 20):
                                    reg.add(yw)
                                    shelves.append(yw)
                                    self._logger.info("Shelf detected at y=%s", yw)

                end_y = await self._my.get_position()
                lim = self._params.motors.by_name("y_axis").lim
                jog_y = await self.get_param("JOG_Y")
                if end_y + jog_y > lim:
                    break
                await self._my.jog(jog_y)

            return shelves

        except ScanTimeoutError as e:
            self._logger.warning(
                "get_shelves timed out after finding %d shelves: %s",
                len(shelves), e
            )
            with sentry_sdk.push_scope() as scope:
                scope.set_tag("operation", "get_shelves")
                scope.set_extra("shelves_found", len(shelves))
                sentry_sdk.capture_message(str(e), level="warning")
            raise
        except CameraError as e:
            self._logger.warning(
                "get_shelves camera error after finding %d shelves: %s",
                len(shelves), e
            )
            sentry_sdk.capture_exception(e)
            raise
        except Exception as e:
            self._logger.exception("get_shelves failed after finding %d shelves", len(shelves))
            sentry_sdk.capture_exception(e)
            raise CameraError(f"Shelf detection failed: {e}") from e

    async def update_grid(
        self, shelves: List[float], timeout_per_shelf: float = GRID_UPDATE_TIMEOUT_PER_SHELF
    ) -> List[float]:
        anchors: List[float] = []

        try:
            await self.home()
            await self._mx.move_to_position(100)
            await self.rotate_to_position("back")
            scan_pos = await self.get_param("SCAN_POSITION")
            await self.move(c.AxisCoordinate(scan_pos[0]-25, scan_pos[1]))

            for shelf_idx, shelf_y in enumerate(shelves):
                start_time = time.monotonic()

                await self._my.move_to_position(shelf_y-26)

                try:
                    frame = await self._cam.capture()
                except Exception as e:
                    raise CameraError(f"Camera capture failed for shelf {shelf_idx}: {e}") from e

                if frame is None:
                    raise CameraError(f"Camera returned empty frame for shelf {shelf_idx}")

                try:
                    frame = self._cam.correct(frame)
                except Exception as e:
                    raise CameraError(f"Frame correction failed for shelf {shelf_idx}: {e}") from e

                cv2.imwrite("frame_correct.png", frame)
                rect = self._cam.detect_white_squares(frame)

                if rect is not None:
                    dx, _ = self._cam.get_center(frame, rect)
                    if dx is not None:
                        pxmm = await self.get_param("PXMM")
                        await self._mx.jog(-dx / pxmm)

                        frame = await self._cam.capture()
                        if frame is None:
                            raise CameraError(f"Camera returned empty frame during correction for shelf {shelf_idx}")

                        frame = self._cam.correct(frame)
                        _, width = frame.shape[:2]
                        third = width // 3
                        frame = frame[:, third:2 * third]
                        rect = self._cam.detect_white_squares(frame)
                        if rect is not None:
                            dx, _ = self._cam.get_center(frame, rect)
                            if dx is not None:
                                await self._mx.jog(-dx / pxmm)
                                posx = await self._mx.get_position()
                                xw = round(posx, 4)
                                anchors.append(xw)
                                self._logger.info("Anchor point for shelf %d at x=%s", shelf_idx, xw)

                elapsed = time.monotonic() - start_time
                if elapsed > timeout_per_shelf:
                    raise ScanTimeoutError(
                        f"Grid update for shelf {shelf_idx} exceeded {timeout_per_shelf}s timeout"
                    )

            return anchors

        except ScanTimeoutError as e:
            self._logger.warning(
                "update_grid timed out after processing %d/%d shelves: %s",
                len(anchors), len(shelves), e
            )
            with sentry_sdk.push_scope() as scope:
                scope.set_tag("operation", "update_grid")
                scope.set_extra("anchors_found", len(anchors))
                scope.set_extra("total_shelves", len(shelves))
                sentry_sdk.capture_message(str(e), level="warning")
            raise
        except CameraError as e:
            self._logger.warning(
                "update_grid camera error after processing %d/%d shelves: %s",
                len(anchors), len(shelves), e
            )
            sentry_sdk.capture_exception(e)
            raise
        except Exception as e:
            self._logger.exception("update_grid failed after processing %d/%d shelves", len(anchors), len(shelves))
            sentry_sdk.capture_exception(e)
            raise CameraError(f"Grid update failed: {e}") from e
    
    async def auto_cal(self):
        """Get shelf positions and anchor points for calibration."""
        # shelves = await self.get_shelves()
        # print(shelves)
        # shelves = [157.704, 381.204, 611.28, 808.724, 957.952, 1108.724]
        shelves =  [59.992, 411.124, 709.3, 1007.82, 1207.96]
        anchors = await self.update_grid(shelves)
        return shelves, anchors
        # todo eventually make this auto generate the slope of each shelf by having two scan positions in x
