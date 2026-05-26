import asyncio
import perpetual_os1.common as c
from perpetual_os1.common import Motor as MotorConfig

from hardware.controller import SharedModbusController as Controller
from hardware.laser import LaserSensor
from hardware.motor import Motor, Servo, MotorStatus, MOTOR_WAIT_TIMEOUT
from hardware.toolcam import ToolCamera
from perpetual_os1.exceptions import (
    MotorError, ConfigError,
    GripperError, MovementError,
    MotorWaitTimeoutError
)

import logging
import time

from enum import Enum
from dataclasses import dataclass
from typing import Optional, Literal, List, Any, Callable


def _create_motor_if_enabled(
    controller: Controller,
    motors: c.Motors,
    name: str,
    safety,
    logger: logging.Logger,
) -> Optional[Motor]:
    try:
        cfg: MotorConfig = motors.by_name(name)
    except KeyError:
        logger.info("Motor '%s' not found in config; skipping", name)
        return None

    if cfg.enabled is False:
        logger.info("Motor '%s' disabled via config; skipping", name)
        return None

    return Motor(controller, cfg, safety)

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
GANTRY_OUTER_TIMEOUT = 120.0
TOOL_OUTER_TIMEOUT = 60.0
ROTATION_HOME_SAFE_X_MM = 475.0
ROTATION_SAFE_X_MARGIN_MM = 60.0
ROTATION_HOME_ORIENTATION_EPSILON_DEG = 1e-6
ROTATION_POSITION_EPSILON_DEG = 0.05

STAGE_RETRACT_TARGET_MM = 3.0
STAGE_RETRACT_SAFE_THRESHOLD_MM = 5.0

class RotationHomeOrientation(Enum):
    LEFT_MODULE = "left_module"
    RIGHT_MODULE = "right_module"


def derive_rotation_home_orientation(
    rotation_front: float,
    rotation_back: float,
    *,
    epsilon_deg: float = ROTATION_HOME_ORIENTATION_EPSILON_DEG,
) -> RotationHomeOrientation:
    front_distance = abs(float(rotation_front))
    back_distance = abs(float(rotation_back))

    if abs(front_distance - back_distance) <= epsilon_deg:
        raise ConfigError(
            "Cannot derive rotation home orientation: "
            "ROTATION_FRONT and ROTATION_BACK are equally close to home"
        )

    if front_distance < back_distance:
        return RotationHomeOrientation.LEFT_MODULE

    return RotationHomeOrientation.RIGHT_MODULE


class Tool:
    def __init__(
        self,
        motorcontroller: Controller,
        lasercontroller: Controller,
        params: c.Tool,
        safety=None,
    ):
        self._motorcontroller = motorcontroller
        self._lasercontroller = lasercontroller
        self._istatus = ToolItemStatus.EMPTY
        self._mstatus = ToolMotionStatus.IDLE
        self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
        self._params = params
        self._guard_paused = False
        self._logger = logging.getLogger(__name__)
        self._last_pick_coord: Optional[c.AxisCoordinate] = None

        motors = self._params.motors
        self._mx: Optional[Motor] = _create_motor_if_enabled(
            motorcontroller, motors, "x_axis", safety, self._logger
        )
        self._my: Optional[Motor] = _create_motor_if_enabled(
            motorcontroller, motors, "y_axis", safety, self._logger
        )
        self._mg: Optional[Motor] = _create_motor_if_enabled(
            motorcontroller, motors, "z_axis", safety, self._logger
        )
        self._ms: Optional[Motor] = _create_motor_if_enabled(
            motorcontroller, motors, "stage", safety, self._logger
        )
        self._mr: Optional[Motor] = _create_motor_if_enabled(
            motorcontroller, motors, "rotate", safety, self._logger
        )
        self._gs = Servo(self._motorcontroller, self._params.gripper.model_dump())

        self._cam = ToolCamera(self._params.poe_camera)
        self._laser_enabled = self._params.laser.enabled
        self._laser_ready = False
        self._laser = None

        if self._laser_enabled is True:
            laser_cfg = self._params.laser
            self._laser = LaserSensor(
                modbus_controller=self._lasercontroller,
                device_id=laser_cfg.device_id,
                read_function_code=laser_cfg.read_function_code,
                register_address=laser_cfg.register_address,
                register_count=laser_cfg.register_count,
                distance_register_index=laser_cfg.distance_register_index,
                distance_scale_mm=laser_cfg.distance_scale_mm,
            )

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
            if self._params.gripper.enabled is True:
                await self._gs.configure()
            if self._my is not None:
                await self._my.configure()
            if self._mx is not None:
                await self._mx.configure()
            if self._mg is not None:
                await self._mg.configure()
            if self._ms is not None:
                await self._ms.configure()
            if self._mr is not None:
                await self._mr.configure()
            if self._laser is not None:
                await self._laser.initialize()
                self._laser_ready = True
            else:
                self._logger.info("Laser disabled via config")
            await self.home()
            self._mstatus = ToolMotionStatus.IDLE
        except Exception as e:
            await self._handle_fault(e)
            raise MotorError(f"Tool configuration failed: {str(e)}") from e

    async def _handle_fault(self, exc: Exception) -> None:
        """Mark the tool as faulted - manual intervention required."""
        self._mstatus = ToolMotionStatus.ERROR
        self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
        self._logger.warning(
            "Tool fault detected - manual intervention required",
            extra={"error": str(exc)},
        )

    async def get_param(self, param: str) -> Optional[Any]:
        """Get a parameter value from tool configuration."""
        try:
            if not hasattr(self._params.parameters, param):
                raise KeyError(f"Parameter '{param}' not found")
            return getattr(self._params.parameters, param)
        except Exception as e:
            raise ConfigError(f"Failed to get parameter {param}: {str(e)}") from e

    def reload_parameters(self) -> None:
        """Reload tool parameters from config.json without restarting."""
        from perpetual_os1.common import load_config_file

        config = load_config_file(c.CONFIG_PATH)
        tool_config = config.get("robot.tool", None)

        if tool_config is None:
            raise ConfigError("robot.tool not found in config")

        self._params = tool_config
        self._logger.info("Tool parameters reloaded from config.json")

    async def home(self) -> None:
        """
        Home all motors in a specific sequence.

        Raises:
            MotorError: If any motor fails to home
        """
        try:
            if self._ms is not None:
                await self._ms.home()
            if self._mg is not None:
                await self._mg.home()
            if self._mr is not None:

                original_x = None
                if self._mx is not None:
                    rotation_front = await self.get_param("ROTATION_FRONT")
                    current_rotation = await self._mr.get_position()

                    if current_rotation > rotation_front:
                        await self._mx.move_to_position(ROTATION_HOME_SAFE_X_MM)
                    else:
                        original_x = await self.ensure_safe_rotation_x_position()

                try:
                    did_extend_stage = await self.ensure_safe_rotation_stage_extension()
                    await self._mr.home()
                    await self.rotate_to_home_orientation(
                        _nested=True,
                        should_ensure_safe_stage_extension=False,
                    )
                finally:
                    await self.restore_safe_rotation_x_position(original_x)
                if did_extend_stage is True and self._ms is not None:
                    await self._ms.home()
            if self._mx is not None:
                await self._mx.home()
            if self._my is not None:
                await self._my.home()
            self._mstatus = ToolMotionStatus.IDLE
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
        except Exception as e:
            await self._handle_fault(e)
            raise MotorError(f"Failed to home tool: {str(e)}") from e

    async def ensure_safe_rotation_stage_extension(self) -> bool:
        if self._ms is None:
            return False

        is_enabled = getattr(self._ms._params, "rotation_safe_stage_extension_enabled", False)
        if is_enabled is not True:
            return False

        safe_extension_mm = self._ms._params.rotation_safe_stage_extension_mm
        stage_limit = self._ms._params.lim
        if safe_extension_mm > stage_limit:
            raise ConfigError(
                "rotation_safe_stage_extension_mm exceeds stage limit "
                f"({safe_extension_mm} > {stage_limit})"
            )

        position = await self._ms.get_position()
        if position >= safe_extension_mm:
            return False

        await self._ms.move_to_position(safe_extension_mm)
        return True

    async def ensure_safe_rotation_x_position(self) -> Optional[float]:
        x_motor = getattr(self, "_mx", None)
        if x_motor is None:
            return None

        margin_mm = ROTATION_SAFE_X_MARGIN_MM
        lower_lim = float(getattr(x_motor._params, "lower_lim", 0.0))
        upper_lim = float(x_motor._params.lim)

        if upper_lim - lower_lim < margin_mm * 2:
            raise ConfigError(
                "X axis limit is too small for safe rotation margin "
                f"({upper_lim - lower_lim} < {margin_mm * 2})"
            )

        current_x = await x_motor.get_position()

        if current_x < lower_lim + margin_mm:
            await x_motor.move_to_position(lower_lim + margin_mm)
            return current_x

        if current_x > upper_lim - margin_mm:
            await x_motor.move_to_position(upper_lim - margin_mm)
            return current_x

        return None

    async def restore_safe_rotation_x_position(self, original_x: Optional[float]) -> None:
        x_motor = getattr(self, "_mx", None)
        if original_x is None or x_motor is None:
            return

        await x_motor.move_to_position(original_x)

    async def is_rotation_at_position(self, target_angle: float) -> bool:
        if self._mr is None:
            raise MotorError("Rotate motor required for rotation")

        current_angle = await self._mr.get_position()
        return abs(float(current_angle) - float(target_angle)) <= ROTATION_POSITION_EPSILON_DEG

    async def _home_stage_after_retract_fault(self, error: BaseException) -> None:
        if self._ms is None:
            raise MotorError("Stage motor required for retract recovery") from error

        self._logger.warning(
            "Stage retract failed; homing stage as recovery",
            extra={"error": str(error)},
        )

        try:
            await self._ms.clear_alarm()
        except Exception as clear_error:
            self._logger.warning(
                "Failed to clear stage alarm before retract recovery home",
                extra={"error": str(clear_error)},
            )

        await self._ms.home()
        stage_position = await self._ms.get_position()

        if stage_position > STAGE_RETRACT_SAFE_THRESHOLD_MM:
            raise MovementError(
                "Stage retract recovery home left stage outside safe threshold "
                f"({stage_position:.3f} > {STAGE_RETRACT_SAFE_THRESHOLD_MM:.3f})"
            ) from error

    async def get_rotation_home_orientation(self) -> RotationHomeOrientation:
        rotation_front = await self.get_param("ROTATION_FRONT")
        rotation_back = await self.get_param("ROTATION_BACK")

        if rotation_front is None or rotation_back is None:
            raise ConfigError("Missing ROTATION_FRONT or ROTATION_BACK parameter")

        return derive_rotation_home_orientation(float(rotation_front), float(rotation_back))

    async def rotate_to_home_orientation(
        self,
        *,
        _nested: bool = False,
        should_ensure_safe_stage_extension: bool = True,
    ) -> None:
        if self._mr is None:
            raise MotorError("Rotate motor required for rotation")

        try:
            rotation_front = await self.get_param("ROTATION_FRONT")
            rotation_back = await self.get_param("ROTATION_BACK")

            if rotation_front is None or rotation_back is None:
                raise ConfigError("Missing ROTATION_FRONT or ROTATION_BACK parameter")

            orientation = derive_rotation_home_orientation(float(rotation_front), float(rotation_back))
            target_angle = float(rotation_front)

            if orientation == RotationHomeOrientation.RIGHT_MODULE:
                target_angle = float(rotation_back)

            if await self.is_rotation_at_position(target_angle):
                return

            if _nested is False:
                self._mstatus = ToolMotionStatus.MOVING
                self._status = ToolStatus(motion=self._mstatus, item=self._istatus)

            original_x = await self.ensure_safe_rotation_x_position()
            try:
                if should_ensure_safe_stage_extension is True:
                    await self.ensure_safe_rotation_stage_extension()

                await self._mr.move_to_position(target_angle)
            finally:
                await self.restore_safe_rotation_x_position(original_x)

            if _nested is False:
                self._mstatus = ToolMotionStatus.IDLE
                self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
        except Exception as e:
            await self._handle_fault(e)
            if isinstance(e, ConfigError):
                raise
            raise MovementError(f"Tool rotation to home orientation failed: {str(e)}") from e

    async def clear_alarms(self) -> None:
        """
        Clear alarms on gripper and stage motors without homing.

        Raises:
            MotorError: If alarm clear fails
        """
        try:
            if self._mg is not None:
                await self._mg.clear_alarm()
            if self._ms is not None:
                await self._ms.clear_alarm()
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
            if self._mx is not None:
                await self._mx.stop()
            if self._mg is not None:
                await self._mg.stop()
            if self._mr is not None:
                await self._mr.stop()
            if self._ms is not None:
                await self._ms.stop()
            if self._my is not None:
                await self._my.stop()
            self._mstatus = ToolMotionStatus.IDLE
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
        except Exception as e:
            await self._handle_fault(e)
            raise MotorError(f"Failed to stop tool: {str(e)}") from e

    async def get_laser_distance(self) -> Optional[float]:
        if self._laser_enabled is False or self._laser is None:
            return None

        if self._laser_ready is False:
            raise ConfigError("Laser is not initialized")

        return await self._laser.read_distance_mm()

    async def capture_laser_scan(
        self,
        *,
        side: int,
        x_positions: List[float],
        y_positions: List[float],
        settle_ms: int,
        samples_per_point: int,
        stage_extension_mm: Optional[float] = None,
        z_extension_mm: Optional[float] = None,
        progress_callback: Optional[Any] = None,
    ) -> List[List[Optional[float]]]:
        if self._laser_enabled is False or self._laser is None:
            raise ConfigError("Laser is not enabled")

        if samples_per_point <= 0:
            raise ConfigError("samples_per_point must be greater than zero")

        if self._mx is None or self._my is None:
            raise MotorError("Gantry motors (x_axis, y_axis) required for laser scan")

        if stage_extension_mm is not None and stage_extension_mm > 0:
            if self._ms is None or self._mg is None:
                raise ConfigError("Tool motors required when stage_extension_mm is configured")

        target_position: Literal["front", "back"] = "front" if side == 0 else "back"
        total_points = len(x_positions) * len(y_positions)
        completed_points = 0
        rows: List[List[Optional[float]]] = []

        try:
            self._mstatus = ToolMotionStatus.MOVING
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)

            await self.retract()
            await self.rotate_to_position(target_position)

            shouldExtendTool = stage_extension_mm is not None and stage_extension_mm > 0

            if shouldExtendTool is True:
                grip_rpm = await self.get_param("Z_MOVE_RPM")
                grip_accel = await self.get_param("Z_MOVE_ACCEL")
                if z_extension_mm is None:
                    grip_value = await self.get_param("Z_EXTENSION_PICK")
                    if grip_value is not None:
                        z_extension_mm = float(grip_value)

                if z_extension_mm is None or grip_rpm is None or grip_accel is None:
                    raise ConfigError("Missing scan extension parameters")

                self._warn_stage_target(stage_extension_mm, "LASER_SCAN_STAGE_EXTENSION")

            for row_idx, y_position in enumerate(y_positions):
                row_values: List[Optional[float]] = [None] * len(x_positions)

                if row_idx % 2 == 0:
                    column_indices = list(range(len(x_positions)))
                else:
                    column_indices = list(range(len(x_positions) - 1, -1, -1))

                row_start_idx = column_indices[0]
                row_start_x = x_positions[row_start_idx]
                await self.move(c.AxisCoordinate(x=row_start_x, y=y_position, s=side))

                if shouldExtendTool is True:
                    await self._ms.ready()
                    await self._ms.move_to_position(stage_extension_mm)
                    await self._mg.move_to_position(z_extension_mm, rpm=grip_rpm, accel=grip_accel)

                for position_idx, column_idx in enumerate(column_indices):
                    x_position = x_positions[column_idx]

                    if position_idx > 0:
                        if shouldExtendTool is True:
                            await self._mx.move_to_position_no_wait(x_position)
                            await self.gantry_wait()
                        else:
                            await self.move(c.AxisCoordinate(x=x_position, y=y_position, s=side))

                    if settle_ms > 0:
                        await asyncio.sleep(settle_ms / 1000.0)

                    sample_values: List[float] = []
                    for _ in range(samples_per_point):
                        depth_mm = await self.get_laser_distance()
                        if depth_mm is not None:
                            sample_values.append(float(depth_mm))

                    if sample_values:
                        sample_values.sort()
                        middle_idx = len(sample_values) // 2
                        row_values[column_idx] = sample_values[middle_idx]

                    completed_points += 1

                    if progress_callback is not None:
                        callback_result = progress_callback(
                            completed_points,
                            total_points,
                            x_position,
                            y_position,
                            row_values[column_idx],
                        )

                        if hasattr(callback_result, "__await__"):
                            await callback_result

                if shouldExtendTool is True:
                    await self.retract()

                rows.append(row_values)
        finally:
            await self.retract()
            self._mstatus = ToolMotionStatus.IDLE
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)

        return rows

    async def _ensure_retracted(self) -> None:
        """
        Ensure gripper and stage are retracted before gantry movement.
        Uses parallel retraction if either axis is extended beyond safe threshold.
        """
        if self._mg is None or self._ms is None:
            return

        SAFE_THRESHOLD = 5.0

        grip_pos = await self._mg.get_position()
        slide_pos = await self._ms.get_position()

        if grip_pos > SAFE_THRESHOLD or slide_pos > SAFE_THRESHOLD:
            self._logger.info(
                "Retracting before gantry move: grip=%.1f, slide=%.1f",
                grip_pos, slide_pos
            )
            await self.retract_empty()

    async def move(self, coordinate: c.AxisCoordinate, *, _nested: bool = False) -> None:
        """
        Move gantry to specified coordinates.

        Args:
            coordinate: Target coordinates
            _nested: Internal flag - when True, doesn't manage motion status

        Raises:
            MovementError: If movement fails or times out
            MotorError: If required gantry motors are disabled
        """
        if self._mx is None or self._my is None:
            raise MotorError("Gantry motors (x_axis, y_axis) required for movement")

        try:
            if _nested is False:
                self._mstatus = ToolMotionStatus.MOVING
                self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
            await self._ensure_retracted()
            await self._my.move_to_position_no_wait(coordinate.y)
            await self._mx.move_to_position_no_wait(coordinate.x)
            await self.gantry_wait()
            if _nested is False:
                self._mstatus = ToolMotionStatus.IDLE
                self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
        except Exception as e:
            await self._handle_fault(e)
            raise MovementError(f"Gantry movement failed: {str(e)}") from e

    async def retract(self, *, _nested: bool = False) -> None:
        """
        Retract gripper and slide to home positions.

        Args:
            _nested: Internal flag - when True, doesn't manage motion status

        Raises:
            MovementError: If retraction fails
            MotorError: If required tool motors are disabled
        """
        if self._mg is None or self._ms is None:
            raise MotorError("Tool motors (z_axis, stage) required for retraction")

        try:
            if _nested is False:
                self._mstatus = ToolMotionStatus.MOVING
                self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
            grip_rpm = await self.get_param("Z_MOVE_RPM")
            grip_accel = await self.get_param("Z_MOVE_ACCEL")
            if grip_rpm is None or grip_accel is None:
                raise ConfigError("Missing gripper move speed")
            await self._mg.move_to_position(1, rpm=grip_rpm, accel=grip_accel)
            try:
                await self._ms.move_to_position(STAGE_RETRACT_TARGET_MM)
            except Exception as stage_error:
                await self._home_stage_after_retract_fault(stage_error)
            await self._ms.idle()
            if _nested is False:
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

    def _warn_stage_target(self, value: float, label: str) -> None:
        """Warn if stage target exceeds motor limits (motor will clamp)."""
        if self._ms is None:
            return
        limit = self._ms._params.lim
        if value < 0 or value > limit:
            self._logger.warning(
                "Stage target will be clamped to motor limits",
                extra={"label": label, "requested_mm": value, "limit_mm": limit},
            )

    async def _get_io_box_stage_extension(self) -> float:
        """Get stage extension for IO box operations."""
        ext = await self.get_param("STAGE_EXTENSION_IO_BOX")

        if ext is None:
            raise ConfigError("Missing STAGE_EXTENSION_IO_BOX parameter")

        return ext

    async def _get_shelf_slide_extension(self, coordinate: Optional[c.AxisCoordinate]) -> float:
        """Select slide extension for shelf pick/place operations."""
        front_ext = await self.get_param("STAGE_EXTENSION_SHELF_FRONT")
        back_ext = await self.get_param("STAGE_EXTENSION_SHELF_BACK")

        if front_ext is None or back_ext is None:
            raise ConfigError("Missing STAGE_EXTENSION_SHELF_FRONT/BACK parameters")

        self._warn_stage_target(front_ext, "STAGE_EXTENSION_SHELF_FRONT")
        self._warn_stage_target(back_ext, "STAGE_EXTENSION_SHELF_BACK")

        if coordinate is None:
            return front_ext

        return front_ext if coordinate.s == 0 else back_ext

    async def rotate_to_position(self, position: Literal["front", "back"], *, _nested: bool = False) -> None:
        """
        Rotate the tool to the configured front or back angle.

        Args:
            position: Target rotation position
            _nested: Internal flag - when True, doesn't manage motion status

        Raises:
            MovementError: If rotation fails
            ConfigError: If rotation parameters are missing
            MotorError: If rotate motor is disabled
        """
        if self._mr is None:
            raise MotorError("Rotate motor required for rotation")

        try:
            param = "ROTATION_FRONT" if position == "front" else "ROTATION_BACK"
            target_angle = await self.get_param(param)
            if target_angle is None:
                raise ConfigError(f"Missing rotation parameter: {param}")

            if await self.is_rotation_at_position(target_angle):
                return

            if _nested is False:
                self._mstatus = ToolMotionStatus.MOVING
                self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
            original_x = await self.ensure_safe_rotation_x_position()
            try:
                await self.ensure_safe_rotation_stage_extension()
                await self._mr.move_to_position(target_angle)
            finally:
                await self.restore_safe_rotation_x_position(original_x)
            if _nested is False:
                self._mstatus = ToolMotionStatus.IDLE
                self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
        except Exception as e:
            await self._handle_fault(e)
            if isinstance(e, ConfigError):
                raise
            raise MovementError(f"Tool rotation failed: {str(e)}") from e

    async def extend(self, coordinate: Optional[c.AxisCoordinate] = None, *, _nested: bool = False) -> None:
        """
        Extend gripper and slide to operation positions.

        Args:
            coordinate: Target coordinates
            _nested: Internal flag - when True, doesn't manage motion status

        Raises:
            MovementError: If extension fails
            ConfigError: If extension parameters are missing
            MotorError: If required tool motors are disabled
        """
        if self._mg is None or self._ms is None:
            raise MotorError("Tool motors (z_axis, stage) required for extension")

        try:
            slide_ext = await self._get_shelf_slide_extension(coordinate)
            grip_ext = await self.get_param("Z_EXTENSION_PLACE")
            grip_rpm = await self.get_param("Z_MOVE_RPM")
            grip_accel = await self.get_param("Z_MOVE_ACCEL")
            if slide_ext is None or grip_ext is None or grip_rpm is None or grip_accel is None:
                raise ConfigError("Missing extension parameters")
            grip_ext = self._with_gripper_offset(grip_ext, coordinate)

            if _nested is False:
                self._mstatus = ToolMotionStatus.MOVING
                self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
            await self._ms.ready()
            await self._ms.move_to_position(slide_ext)
            await self._mg.move_to_position(grip_ext, rpm=grip_rpm, accel=grip_accel)
            if _nested is False:
                self._mstatus = ToolMotionStatus.IDLE
                self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
        except Exception as e:
            await self._handle_fault(e)
            if isinstance(e, ConfigError):
                raise
            raise MovementError(f"Tool extension failed: {str(e)}") from e

    async def extend_IO(self, nudge_override: float | None = None) -> None:
        """
        Extend gripper and slide to IO box position, then nudge to push item.

        Raises:
            MovementError: If extension fails
            ConfigError: If extension parameters are missing
            MotorError: If required tool motors are disabled
        """
        if self._mg is None or self._ms is None:
            raise MotorError("Tool motors (z_axis, stage) required for IO extension")

        slide_ext = await self._get_io_box_stage_extension()
        stage_nudge = nudge_override if nudge_override is not None else await self.get_param("STAGE_NUDGE")
        grip_ext = await self.get_param("Z_EXTENSION_IO")
        grip_rpm = await self.get_param("Z_MOVE_RPM")
        grip_accel = await self.get_param("Z_MOVE_ACCEL")

        if stage_nudge is None or grip_ext is None or grip_rpm is None or grip_accel is None:
            raise ConfigError("Missing IO box extension parameters")

        self._warn_stage_target(slide_ext, "STAGE_EXTENSION_IO_BOX")
        self._warn_stage_target(slide_ext + stage_nudge, "STAGE_EXTENSION_IO_BOX + STAGE_NUDGE")

        try:
            self._mstatus = ToolMotionStatus.MOVING
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
            await self._ms.ready()
            await self._ms.move_to_position(slide_ext)
            await self._mg.move_to_position(grip_ext, rpm=grip_rpm, accel=grip_accel)
            await self._ms.move_to_position(slide_ext + stage_nudge)
            self._mstatus = ToolMotionStatus.IDLE
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
        except Exception as e:
            await self._handle_fault(e)
            if isinstance(e, ConfigError):
                raise
            raise MovementError(f"Tool IO extension failed: {str(e)}") from e

    async def extend_empty(self, coordinate: Optional[c.AxisCoordinate] = None) -> None:
        """
        Extend empty tool without waiting between moves.

        Note: This is a helper operation that doesn't manage motion status.
        The caller is responsible for setting status to MOVING before calling
        and IDLE after the complete operation finishes.

        Raises:
            MovementError: If extension fails
            ConfigError: If extension parameters are missing
            MotorError: If required tool motors are disabled
        """
        if self._mg is None or self._ms is None:
            raise MotorError("Tool motors (z_axis, stage) required for empty extension")

        try:
            slide_ext = await self._get_shelf_slide_extension(coordinate)
            grip_ext = await self.get_param("Z_EXTENSION_PICK")
            grip_rpm = await self.get_param("Z_MOVE_RPM")
            grip_accel = await self.get_param("Z_MOVE_ACCEL")
            if slide_ext is None or grip_ext is None or grip_rpm is None or grip_accel is None:
                raise ConfigError("Missing extension parameters")
            grip_ext = self._with_gripper_offset(grip_ext, coordinate)

            await self._ms.ready()
            await self._ms.move_to_position_no_wait(slide_ext)
            await self._mg.move_to_position_no_wait(grip_ext, rpm=grip_rpm, accel=grip_accel)
            await self.tool_wait(homing=False)
        except Exception as e:
            await self._handle_fault(e)
            if isinstance(e, ConfigError):
                raise
            raise MovementError(f"Tool empty extension failed: {str(e)}") from e

    async def retract_empty(self) -> None:
        """
        Retract empty tool without waiting between moves.

        Note: This is a helper operation that doesn't manage motion status.
        The caller is responsible for setting status to MOVING before calling
        and IDLE after the complete operation finishes.

        Raises:
            MovementError: If retraction fails
            MotorError: If required tool motors are disabled
        """
        if self._mg is None or self._ms is None:
            raise MotorError("Tool motors (z_axis, stage) required for empty retraction")

        try:
            await self._ms.move_to_position_no_wait(3.0)
            grip_rpm = await self.get_param("Z_MOVE_RPM")
            grip_accel = await self.get_param("Z_MOVE_ACCEL")
            if grip_rpm is None or grip_accel is None:
                raise ConfigError("Missing gripper move speed")
            await self._mg.move_to_position_no_wait(1, rpm=grip_rpm, accel=grip_accel)
            await self.tool_wait(homing=True)
            await self._ms.idle()
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
            self._last_pick_coord = coordinate

            target_position: Literal["front", "back"] = "front" if coordinate.s == 0 else "back"

            await self._gs.safe_open()
            await self.move(coordinate, _nested=True)
            await self.rotate_to_position(target_position, _nested=True)
            await self.extend_empty(coordinate)
            await self._gs.open()
            await self.retract(_nested=True)

            self._istatus = ToolItemStatus.LOADED
            self._mstatus = ToolMotionStatus.IDLE
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
        except Exception as e:
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

            target_position: Literal["front", "back"] = "front" if coordinate.s == 0 else "back"

            await self.move(coordinate, _nested=True)
            await self.rotate_to_position(target_position, _nested=True)
            await self.extend(coordinate, _nested=True)
            await self._gs.close()
            await self.retract_empty()

            self._istatus = ToolItemStatus.EMPTY
            self._mstatus = ToolMotionStatus.IDLE
            self._status = ToolStatus(motion=self._mstatus, item=self._istatus)
            self._last_pick_coord = None
        except Exception as e:
            await self._handle_fault(e)
            if isinstance(e, MovementError):
                raise
            raise GripperError(f"Place operation failed: {str(e)}") from e

    async def gantry_wait(
        self,
        timeout_seconds: float = GANTRY_OUTER_TIMEOUT,
        should_abort: Callable[[], bool] | None = None,
    ) -> None:
        """
        Wait for gantry motors to complete movement.

        Raises:
            MotorWaitTimeoutError: If outer timeout exceeded
            MovementError: If motors error during wait
            MotorError: If required gantry motors are disabled
        """
        if self._mx is None or self._my is None:
            raise MotorError("Gantry motors (x_axis, y_axis) required for gantry wait")

        start_time = time.monotonic()
        terminal_error_states = {MotorStatus.ERROR, MotorStatus.FAULT, MotorStatus.TIMEOUT}

        try:
            while True:
                if time.monotonic() - start_time > timeout_seconds:
                    raise MotorWaitTimeoutError(f"Gantry wait exceeded {timeout_seconds}s timeout")

                mx_f = await self._mx.wait_for_move(timeout=GANTRY_WAIT_TIMEOUT, should_abort=should_abort)
                my_f = await self._my.wait_for_move(timeout=GANTRY_WAIT_TIMEOUT, should_abort=should_abort)

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

    async def tool_wait(
        self,
        homing: bool,
        timeout_seconds: float = TOOL_OUTER_TIMEOUT,
        should_abort: Callable[[], bool] | None = None,
    ) -> None:
        """
        Wait for tool motors to complete movement.

        Raises:
            MotorWaitTimeoutError: If outer timeout exceeded
            MovementError: If motors error during wait
            MotorError: If required tool motors are disabled
        """
        if self._mg is None or self._ms is None:
            raise MotorError("Tool motors (z_axis, stage) required for tool wait")

        start_time = time.monotonic()
        terminal_error_states = {MotorStatus.ERROR, MotorStatus.FAULT, MotorStatus.TIMEOUT}

        try:
            while True:
                if time.monotonic() - start_time > timeout_seconds:
                    raise MotorWaitTimeoutError(f"Tool wait exceeded {timeout_seconds}s timeout")

                mg_f = await self._mg.wait_for_move(
                    timeout=TOOL_WAIT_TIMEOUT,
                    homing=homing,
                    should_abort=should_abort,
                )
                ms_f = await self._ms.wait_for_move(
                    timeout=TOOL_WAIT_TIMEOUT,
                    homing=homing,
                    should_abort=should_abort,
                )

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
        x = await self._mx.get_position() if self._mx is not None else 0.0
        y = await self._my.get_position() if self._my is not None else 0.0
        g = await self._mg.get_position() if self._mg is not None else 0.0
        s = await self._ms.get_position() if self._ms is not None else 0.0
        r = await self._mr.get_position() if self._mr is not None else 0.0
        return (c.AxisCoordinate(x, y), c.ToolCoordinate(g, s, r))
