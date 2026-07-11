import asyncio
import functools
import logging
import time

import openshelf_os.common as c

from enum import Enum
from typing import Optional, Literal, List, Any, Callable
from openshelf_os.common import Motor as MotorConfig
from hardware.controller import SharedModbusController as Controller
from hardware.laser import LaserSensor
from hardware.motor import Motor, MotorStatus, MOTOR_WAIT_TIMEOUT
from hardware.servo import Servo
from openshelf_os.common import load_config_file
from openshelf_os.exceptions import (
    MotorError, ConfigError,
    GripperError, MovementError,
    MotorWaitTimeoutError
)


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
    # ------------------------------------------------------------------ #
    #  Lifecycle
    # ------------------------------------------------------------------ #

    def __init__(
        self,
        motorcontroller: Controller,
        lasercontroller: Controller,
        params: c.Tool,
        safety=None,
    ):
        self._motorcontroller = motorcontroller
        self._lasercontroller = lasercontroller
        self._params = params
        self._guard_paused = False
        self._logger = logging.getLogger(__name__)
        self._init_reporter: Optional[Callable[[str, str, str, Optional[str]], None]] = None

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

    def _emit_init(self, component: str, phase: str, detail: str = "", reason: Optional[str] = None) -> None:
        reporter = getattr(self, "_init_reporter", None)
        if reporter is None:
            return
        try:
            reporter(component, phase, detail, reason)
        except Exception:
            pass

    async def _configure_axis_isolated(self, motor: Optional[Motor], isolate: bool) -> bool:
        if motor is None:
            return True
        if isolate is False:
            await motor.configure()
            return True
        try:
            await motor.configure()
            return True
        except Exception as e:
            self._logger.warning("init motor configure failed: %s", e)
            return False

    def _mark_configured_motors_ready(self, failed_config_motors: List[Motor]) -> None:
        for component, motor in (
            ("x_axis", self._mx),
            ("y_axis", self._my),
            ("z_axis", self._mg),
            ("stage", self._ms),
            ("rotate", self._mr),
        ):
            if motor is None or self._did_motor_configure_fail(motor, failed_config_motors) is True:
                continue
            self._emit_init(component, "ready")

    async def configure(self, isolate: bool = False, home_on_init: bool = True):
        try:
            if self._params.gripper.enabled is True:
                if isolate is True:
                    try:
                        await self._gs.configure()
                    except Exception as e:
                        self._logger.warning("init gripper configure failed: %s", e)
                else:
                    await self._gs.configure()

            failed_config_motors = []
            for motor in (self._my, self._mx, self._mg, self._ms, self._mr):
                did_configure = await self._configure_axis_isolated(motor, isolate)
                if did_configure is False and motor is not None:
                    failed_config_motors.append(motor)

            if self._laser is not None:
                self._emit_init("laser", "configuring")
                try:
                    await self._laser.initialize()
                    self._laser_ready = True
                    self._emit_init("laser", "ready")
                except Exception as e:
                    self._emit_init("laser", "failed", reason=str(e))
                    if isolate is False:
                        raise
            else:
                self._logger.info("Laser disabled via config")

            if home_on_init is True:
                await self.home(isolate=isolate, failed_config_motors=failed_config_motors)
            else:
                self._mark_configured_motors_ready(failed_config_motors)
        except Exception as e:
            await self._handle_fault(e)

            raise MotorError(f"Tool configuration failed: {str(e)}") from e

    async def stop(self) -> None:
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

        except Exception as e:
            await self._handle_fault(e)

            raise MotorError(f"Failed to stop tool: {str(e)}") from e

    async def restore_y_brake_output(self) -> None:
        if self._my is None:
            return

        await self._my.restore_brake_output()

    async def clear_alarms(self) -> None:
        try:
            if self._mg is not None:
                await self._mg.clear_alarm()
            if self._ms is not None:
                await self._ms.clear_alarm()

        except Exception as e:
            await self._handle_fault(e)
            raise MotorError(f"Failed to clear alarms: {str(e)}") from e

    def block_motion_for_safety(self) -> None:
        if self._guard_paused:
            return

        self._guard_paused = True
        self._logger.info("Safety guard pausing tool")

    def release_motion_safety(self) -> None:
        if not self._guard_paused:
            return

        self._guard_paused = False
        self._logger.info("Safety guard cleared for tool")

    def set_motion_diagnostic_context(
        self,
        *,
        run_id: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        for motor in (self._mx, self._my, self._mg, self._ms, self._mr):
            if motor is not None:
                motor.set_diagnostic_context(run_id=run_id, correlation_id=correlation_id)

    # ------------------------------------------------------------------ #
    #  Fault, status, and configuration
    # ------------------------------------------------------------------ #

    def _snapshot_motor(self, axis: str, motor: Optional[Motor]) -> dict[str, Any]:
        snapshot: dict[str, Any] = {"axis": axis, "configured": motor is not None}
        if motor is None:
            return snapshot

        for key, attr in (
            ("motor_id", "_motor_id"),
            ("position_mm", "_cur_pos"),
            ("target_mm", "_target_pos"),
            ("simulated_fault", "_simulated_fault"),
        ):
            try:
                snapshot[key] = getattr(motor, attr, None)
            except Exception as exc:
                snapshot[f"{key}_error"] = str(exc)

        try:
            status = getattr(motor, "_status", None)
            snapshot["status"] = getattr(status, "name", status)
        except Exception as exc:
            snapshot["status_error"] = str(exc)

        for key, reader in (("homed", motor.is_homed), ("hard_stopped", motor.is_hard_stopped)):
            try:
                snapshot[key] = reader()
            except Exception as exc:
                snapshot[f"{key}_error"] = str(exc)

        return snapshot

    @staticmethod
    def _safe_dump(value: Any) -> Any:
        if value is None:
            return None
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json")
        if hasattr(value, "__dataclass_fields__"):
            return {key: Tool._safe_dump(getattr(value, key)) for key in value.__dataclass_fields__}
        if isinstance(value, dict):
            return {key: Tool._safe_dump(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [Tool._safe_dump(item) for item in value]
        return value


    def _motor_snapshot(self) -> dict[str, dict[str, Any]]:
        motors = {
            "x_axis": getattr(self, "_mx", None),
            "y_axis": getattr(self, "_my", None),
            "z_axis": getattr(self, "_mg", None),
            "stage": getattr(self, "_ms", None),
            "rotate": getattr(self, "_mr", None),
        }
        return {axis: self._snapshot_motor(axis, motor) for axis, motor in motors.items()}

    async def _handle_fault(
        self,
        exc: Exception,
        *,
        phase: str | None = None,
        coordinate: Any = None,
        axis_statuses: dict[str, Any] | None = None,
        target_position: Any = None,
    ) -> None:
        existing_context = getattr(exc, "fault_context", None)
        if existing_context is None:
            cause = getattr(exc, "__cause__", None)
            existing_context = getattr(cause, "fault_context", None)

        previous_context = dict(existing_context) if isinstance(existing_context, dict) else None

        context: dict[str, Any] = previous_context.copy() if previous_context is not None else {}
        context.update(
            {
                "exception_type": type(exc).__name__,
                "exception_message": str(exc),
                "motors": self._motor_snapshot(),
            }
        )

        if phase is not None:
            if previous_context is not None and previous_context.get("phase") != phase:
                context["previous_fault_context"] = previous_context
            context["phase"] = phase
        if coordinate is not None:
            context["coordinate"] = self._safe_dump(coordinate)
        if axis_statuses is not None:
            context["axis_statuses"] = axis_statuses
        if target_position is not None:
            context["target_position"] = target_position

        setattr(exc, "fault_context", context)
        log_warning = getattr(self._logger, "warning", None)
        if log_warning is not None:
            log_warning(
                "Tool fault detected - manual intervention required",
                extra=context,
            )

    async def get_param(self, param: str) -> Optional[Any]:
        try:
            if not hasattr(self._params.parameters, param):
                raise KeyError(f"Parameter '{param}' not found")

            return getattr(self._params.parameters, param)
        except Exception as e:
            raise ConfigError(f"Failed to get parameter {param}: {str(e)}") from e

    def reload_parameters(self) -> None:
        config = load_config_file(c.CONFIG_PATH)
        tool_config = config.get("robot.tool", None)

        if tool_config is None:
            raise ConfigError("robot.tool not found in config")

        self._params = tool_config
        self._logger.info("Tool parameters reloaded from config.json")


    # ------------------------------------------------------------------ #
    #  High-level operations
    # ------------------------------------------------------------------ #

    async def pick(self, coordinate: c.AxisCoordinate) -> None:
        try:
            target_position: Literal["front", "back"] = "front" if coordinate.s == 0 else "back"

            await self._gs.safe_open()
            await self.move(coordinate)
            await self.rotate_to_position(target_position)
            await self.extend_empty(coordinate)
            await self._gs.open()
            await self.retract()

        except Exception as e:
            await self._handle_fault(e)
            if isinstance(e, MovementError):
                raise
            raise GripperError(f"Pick operation failed: {str(e)}") from e

    async def place(self, coordinate: c.AxisCoordinate):
        try:
            target_position: Literal["front", "back"] = "front" if coordinate.s == 0 else "back"

            await self.move(coordinate)
            await self.rotate_to_position(target_position)
            await self.extend(coordinate)
            await self._gs.close()
            await self.retract_empty()

        except Exception as e:
            await self._handle_fault(e)

            if isinstance(e, MovementError):
                raise

            raise GripperError(f"Place operation failed: {str(e)}") from e

    # ------------------------------------------------------------------ #
    #  Laser
    # ------------------------------------------------------------------ #

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
            await self.retract()
            await self.rotate_to_position(target_position)

            shouldExtendTool = stage_extension_mm is not None and stage_extension_mm > 0

            if shouldExtendTool is True:
                assert stage_extension_mm is not None
                stage_motor = self._ms
                z_motor = self._mg
                assert stage_motor is not None
                assert z_motor is not None
                stage_scan_extension_mm = float(stage_extension_mm)
                grip_rpm = await self.get_param("Z_MOVE_RPM")
                grip_accel = await self.get_param("Z_MOVE_ACCEL")

                if z_extension_mm is None:
                    grip_value = await self.get_param("Z_EXTENSION_PICK")
                    if grip_value is not None:
                        z_extension_mm = float(grip_value)

                if z_extension_mm is None or grip_rpm is None or grip_accel is None:
                    raise ConfigError("Missing scan extension parameters")

                z_scan_extension_mm = float(z_extension_mm)
                self._warn_stage_target(stage_scan_extension_mm, "LASER_SCAN_STAGE_EXTENSION")

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
                    assert stage_motor is not None
                    assert z_motor is not None
                    await stage_motor.ready()
                    await stage_motor.move_to_position(stage_scan_extension_mm)
                    await z_motor.move_to_position(z_scan_extension_mm, rpm=grip_rpm, accel=grip_accel)

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

        return rows

    # ------------------------------------------------------------------ #
    #  Gantry movement
    # ------------------------------------------------------------------ #

    async def move(self, coordinate: c.AxisCoordinate) -> None:
        if self._mx is None or self._my is None:
            raise MotorError("Gantry motors (x_axis, y_axis) required for movement")

        try:
            await self._ensure_retracted()
            await self._my.move_to_position_no_wait(coordinate.y)
            await self._mx.move_to_position_no_wait(coordinate.x)
            await self.gantry_wait()

        except Exception as e:
            await self._handle_fault(e, phase="gantry.move", coordinate=coordinate)
            err = MovementError(f"Gantry movement failed: {str(e)}")
            if hasattr(e, "fault_context"):
                setattr(err, "fault_context", getattr(e, "fault_context"))
            raise err from e

    async def gantry_wait(
        self,
        timeout_seconds: float = GANTRY_OUTER_TIMEOUT,
        should_abort: Callable[[], bool] | None = None,
        fast_complete_at_target: bool = False,
    ) -> None:
        if self._mx is None or self._my is None:
            raise MotorError("Gantry motors (x_axis, y_axis) required for gantry wait")

        start_time = time.monotonic()
        terminal_error_states = {MotorStatus.ERROR, MotorStatus.FAULT, MotorStatus.TIMEOUT}
        axis_statuses: dict[str, str] = {}

        try:
            remaining_timeout = max(0.1, min(GANTRY_WAIT_TIMEOUT, timeout_seconds))
            x_task = asyncio.create_task(
                self._mx.wait_for_move(
                    timeout=remaining_timeout,
                    should_abort=should_abort,
                    fast_complete_at_target=fast_complete_at_target,
                )
            )
            y_task = asyncio.create_task(
                self._my.wait_for_move(
                    timeout=remaining_timeout,
                    should_abort=should_abort,
                    fast_complete_at_target=fast_complete_at_target,
                )
            )
            tasks = {x_task: ("x_axis", self._mx), y_task: ("y_axis", self._my)}
            statuses: dict[str, MotorStatus] = {}

            def build_axis_statuses() -> dict[str, str]:
                return {name: item.name for name, item in statuses.items()}

            async def stop_pending(
                pending_tasks: set[asyncio.Task[MotorStatus]],
            ) -> list[str]:
                cleanup_tasks = []
                cleanup_axes = []

                for pending_task in pending_tasks:
                    axis, motor = tasks[pending_task]
                    cleanup_method = getattr(motor, "_stop_and_clear_motion", None)

                    if cleanup_method is None:
                        async def cleanup_motor(item: Motor) -> None:
                            await item.stop()
                            clear_motion = getattr(item, "_clear_pending_motion", None)
                            if clear_motion is not None:
                                clear_motion()
                            else:
                                setattr(item, "_pending_motion", None)
                                setattr(item, "_target_pos", None)

                        cleanup_method = functools.partial(cleanup_motor, motor)

                    cleanup_tasks.append(cleanup_method())
                    cleanup_axes.append(axis)

                results = await asyncio.gather(*cleanup_tasks, return_exceptions=True)
                stop_errors = []

                for axis, result in zip(cleanup_axes, results):
                    if isinstance(result, Exception):
                        statuses[axis] = MotorStatus.ERROR
                        stop_errors.append(f"{axis}={str(result)}")
                        continue

                    statuses.setdefault(axis, MotorStatus.STOPPED)

                return stop_errors

            async def stop_and_drain_pending(
                pending_tasks: set[asyncio.Task[MotorStatus]],
            ) -> None:
                nonlocal axis_statuses

                stop_errors = await stop_pending(pending_tasks)
                for pending_task in pending_tasks:
                    pending_task.cancel()
                await asyncio.gather(*pending_tasks, return_exceptions=True)

                if stop_errors:
                    axis_statuses = build_axis_statuses()
                    raise MovementError(
                        "Failed to stop pending gantry motors: "
                        + ", ".join(stop_errors)
                    )

            try:
                while tasks:
                    remaining_timeout = timeout_seconds - (time.monotonic() - start_time)

                    if remaining_timeout <= 0:
                        await stop_and_drain_pending(set(tasks))
                        raise MotorWaitTimeoutError(f"Gantry wait exceeded {timeout_seconds}s timeout")

                    done, pending = await asyncio.wait(
                        set(tasks),
                        timeout=remaining_timeout,
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    if not done:
                        await stop_and_drain_pending(pending)
                        raise MotorWaitTimeoutError(f"Gantry wait exceeded {timeout_seconds}s timeout")

                    for task in done:
                        axis, _motor = tasks.pop(task)

                        if task.cancelled() is True:
                            await stop_and_drain_pending(pending)
                            raise asyncio.CancelledError()

                        exc = task.exception()
                        if exc is not None:
                            await stop_and_drain_pending(pending)
                            raise exc

                        statuses[axis] = task.result()

                    if any(status in terminal_error_states for status in statuses.values()):
                        await stop_and_drain_pending(pending)
                        axis_statuses = build_axis_statuses()
                        raise MovementError(
                            "Gantry motor error during wait: "
                            f"x={axis_statuses.get('x_axis', 'UNKNOWN')}, "
                            f"y={axis_statuses.get('y_axis', 'UNKNOWN')}"
                        )

                    if any(status != MotorStatus.STOPPED for status in statuses.values()):
                        await stop_and_drain_pending(pending)
                        axis_statuses = build_axis_statuses()
                        raise MovementError(
                            "Gantry movement ended unexpectedly: "
                            f"x={axis_statuses.get('x_axis', 'UNKNOWN')}, "
                            f"y={axis_statuses.get('y_axis', 'UNKNOWN')}"
                        )
            except asyncio.CancelledError:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise

            axis_statuses = build_axis_statuses()

            log_info = getattr(self._logger, "info", None)
            if log_info is not None:
                log_info(
                    "Gantry wait completed",
                    extra={
                        "axis_statuses": axis_statuses,
                        "elapsed_s": time.monotonic() - start_time,
                    },
                )

            return
        except MotorWaitTimeoutError:
            raise
        except MovementError as e:
            await self._handle_fault(
                e,
                phase="gantry.wait",
                axis_statuses=axis_statuses,
            )
            raise
        except Exception as e:
            await self._handle_fault(e, phase="gantry.wait", axis_statuses=axis_statuses)
            raise MovementError(f"Gantry wait failed: {str(e)}") from e

    async def _ensure_retracted(self) -> None:
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

    # ------------------------------------------------------------------ #
    #  Rotation
    # ------------------------------------------------------------------ #

    async def rotate_to_position(self, position: Literal["front", "back"]) -> None:
        if self._mr is None:
            raise MotorError("Rotate motor required for rotation")

        try:
            param = "ROTATION_FRONT" if position == "front" else "ROTATION_BACK"
            target_angle = await self.get_param(param)

            if target_angle is None:
                raise ConfigError(f"Missing rotation parameter: {param}")

            if await self.is_rotation_at_position(target_angle):
                return


            original_x = await self.ensure_safe_rotation_x_position()

            try:
                await self.ensure_safe_rotation_stage_extension()
                await self._mr.move_to_position(target_angle)
            finally:
                await self.restore_safe_rotation_x_position(original_x)

        except Exception as e:
            await self._handle_fault(e)

            if isinstance(e, ConfigError):
                raise

            raise MovementError(f"Tool rotation failed: {str(e)}") from e

    async def rotate_to_home_orientation(
        self,
        *,
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


            original_x = await self.ensure_safe_rotation_x_position()

            try:
                if should_ensure_safe_stage_extension is True:
                    await self.ensure_safe_rotation_stage_extension()

                await self._mr.move_to_position(target_angle)
            finally:
                await self.restore_safe_rotation_x_position(original_x)

        except Exception as e:
            await self._handle_fault(e)

            if isinstance(e, ConfigError):
                raise

            raise MovementError(f"Tool rotation to home orientation failed: {str(e)}") from e


    async def is_rotation_at_position(self, target_angle: float) -> bool:
        if self._mr is None:
            raise MotorError("Rotate motor required for rotation")

        current_angle = await self._mr.get_position()

        return abs(float(current_angle) - float(target_angle)) <= ROTATION_POSITION_EPSILON_DEG

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

    # ------------------------------------------------------------------ #
    #  Tool extension and retraction
    # ------------------------------------------------------------------ #

    async def extend(self, coordinate: Optional[c.AxisCoordinate] = None) -> None:
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


            await self._ms.ready()
            await self._ms.move_to_position(slide_ext)
            await self._mg.move_to_position(grip_ext, rpm=grip_rpm, accel=grip_accel)

        except Exception as e:
            await self._handle_fault(e)

            if isinstance(e, ConfigError):
                raise

            raise MovementError(f"Tool extension failed: {str(e)}") from e

    async def extend_IO(self, nudge_override: float | None = None) -> None:
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
            await self._ms.ready()
            await self._ms.move_to_position(slide_ext)
            await self._mg.move_to_position(grip_ext, rpm=grip_rpm, accel=grip_accel)
            await self._ms.move_to_position(slide_ext + stage_nudge)
        except Exception as e:
            await self._handle_fault(e)

            if isinstance(e, ConfigError):
                raise

            raise MovementError(f"Tool IO extension failed: {str(e)}") from e

    async def extend_empty(self, coordinate: Optional[c.AxisCoordinate] = None) -> None:
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

    async def retract(self) -> None:
        if self._mg is None or self._ms is None:
            raise MotorError("Tool motors (z_axis, stage) required for retraction")

        try:
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

        except Exception as e:
            await self._handle_fault(e)

            raise MovementError(f"Tool retraction failed: {str(e)}") from e

    async def retract_empty(self) -> None:
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

    async def tool_wait(
        self,
        homing: bool,
        timeout_seconds: float = TOOL_OUTER_TIMEOUT,
        should_abort: Callable[[], bool] | None = None,
    ) -> None:
        if self._mg is None or self._ms is None:
            raise MotorError("Tool motors (z_axis, stage) required for tool wait")

        start_time = time.monotonic()
        terminal_error_states = {MotorStatus.ERROR, MotorStatus.FAULT, MotorStatus.TIMEOUT}
        axis_statuses: dict[str, str] = {}

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
                axis_statuses = {"z_axis": mg_f.name, "stage": ms_f.name}

                if mg_f == MotorStatus.STOPPED and ms_f == MotorStatus.STOPPED:
                    return

                if mg_f in terminal_error_states or ms_f in terminal_error_states:
                    raise MovementError(f"Tool motor error during wait: gripper={mg_f.name}, stage={ms_f.name}")

        except MotorWaitTimeoutError:
            raise

        except MovementError as e:
            await self._handle_fault(
                e,
                phase="tool.wait",
                axis_statuses=axis_statuses,
            )
            raise

        except Exception as e:
            await self._handle_fault(e)
            raise MovementError(f"Tool wait failed: {str(e)}") from e

    def _with_gripper_offset(self, value: float, coordinate: Optional[c.AxisCoordinate]) -> float:
        if coordinate is None:
            return value

        return value + getattr(coordinate, "z_offset", 0.0)

    def _warn_stage_target(self, value: float, label: str) -> None:
        if self._ms is None:
            return

        limit = self._ms._params.lim

        if value < 0 or value > limit:
            self._logger.warning(
                "Stage target will be clamped to motor limits",
                extra={"label": label, "requested_mm": value, "limit_mm": limit},
            )

    async def _get_io_box_stage_extension(self) -> float:
        ext = await self.get_param("STAGE_EXTENSION_IO_BOX")

        if ext is None:
            raise ConfigError("Missing STAGE_EXTENSION_IO_BOX parameter")

        return ext

    async def _get_shelf_slide_extension(self, coordinate: Optional[c.AxisCoordinate]) -> float:
        front_ext = await self.get_param("STAGE_EXTENSION_SHELF_FRONT")
        back_ext = await self.get_param("STAGE_EXTENSION_SHELF_BACK")

        if front_ext is None or back_ext is None:
            raise ConfigError("Missing STAGE_EXTENSION_SHELF_FRONT/BACK parameters")

        self._warn_stage_target(front_ext, "STAGE_EXTENSION_SHELF_FRONT")
        self._warn_stage_target(back_ext, "STAGE_EXTENSION_SHELF_BACK")

        if coordinate is None:
            return front_ext

        return front_ext if coordinate.s == 0 else back_ext

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

    # ------------------------------------------------------------------ #
    #  Homing
    # ------------------------------------------------------------------ #

    async def _home_rotate_sequence(self) -> None:
        if self._mr is None:
            return

        rotate_motor = self._mr
        original_x = None

        if self._mx is not None:
            rotation_front = await self.get_param("ROTATION_FRONT")

            if rotation_front is None:
                raise ConfigError("Missing ROTATION_FRONT parameter")

            current_rotation = await rotate_motor.get_position()

            if current_rotation > float(rotation_front):
                await self._mx.move_to_position(ROTATION_HOME_SAFE_X_MM)
            else:
                original_x = await self.ensure_safe_rotation_x_position()

        try:
            did_extend_stage = await self.ensure_safe_rotation_stage_extension()
            await rotate_motor.home()
            await self.rotate_to_home_orientation(
                should_ensure_safe_stage_extension=False,
            )
        finally:
            await self.restore_safe_rotation_x_position(original_x)

        if did_extend_stage is True and self._ms is not None:
            await self._ms.home()

    async def _home_sequence(self) -> None:
        if self._ms is not None:
            await self._ms.home()

        if self._mg is not None:
            await self._mg.home()

        if self._mr is not None:
            await self._home_rotate_sequence()

        if self._mx is not None:
            await self._mx.home()

        if self._my is not None:
            await self._my.home()

    def _did_motor_configure_fail(self, motor: Optional[Motor], failed_config_motors: List[Motor]) -> bool:
        if motor is None:
            return False
        return any(failed_motor is motor for failed_motor in failed_config_motors)

    async def _home_step_isolated(self, motor: Optional[Motor], failed_config_motors: List[Motor]) -> bool:
        if motor is None:
            return True
        if self._did_motor_configure_fail(motor, failed_config_motors) is True:
            return True
        try:
            await motor.home()
            return True
        except Exception as e:
            self._logger.warning("init home step failed: %s", e)
            return False

    async def _home_rotate_isolated(
        self,
        failed_config_motors: List[Motor],
        failed_home_motors: Optional[List[Motor]] = None,
    ) -> None:
        if self._mr is None:
            return
        if self._did_motor_configure_fail(self._mr, failed_config_motors) is True:
            return
        failed_home_motors = failed_home_motors or []
        if any(
            self._did_motor_configure_fail(motor, failed_config_motors) is True
            or any(failed_home_motor is motor for failed_home_motor in failed_home_motors)
            for motor in (self._mx, self._ms)
        ):
            self._emit_init("rotate", "failed", "home", "dependency failed")
            return
        try:
            await self._home_rotate_sequence()
        except Exception as e:
            self._emit_init("rotate", "failed", "home", str(e))
            self._logger.warning("init rotate home failed: %s", e)

    async def home(self, isolate: bool = False, failed_config_motors: Optional[List[Motor]] = None) -> None:
        if isolate is False:
            try:
                await self.restore_y_brake_output()
                await self._home_sequence()
            except Exception as e:
                await self._handle_fault(e)

                raise MotorError(f"Failed to home tool: {str(e)}") from e
            return

        failed_config_motors = failed_config_motors or []

        try:
            await self.restore_y_brake_output()
        except Exception as e:
            self._logger.warning("init restore_y_brake_output failed: %s", e)

        failed_home_motors = []
        if await self._home_step_isolated(self._ms, failed_config_motors) is False and self._ms is not None:
            failed_home_motors.append(self._ms)
        if await self._home_step_isolated(self._mg, failed_config_motors) is False and self._mg is not None:
            failed_home_motors.append(self._mg)
        if await self._home_step_isolated(self._mx, failed_config_motors) is False and self._mx is not None:
            failed_home_motors.append(self._mx)
        await self._home_rotate_isolated(failed_config_motors, failed_home_motors)
        await self._home_step_isolated(self._my, failed_config_motors)
