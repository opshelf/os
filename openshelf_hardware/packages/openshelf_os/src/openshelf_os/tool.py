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
ROTATION_SAFE_X_MARGIN_MM = 200.0
ROTATION_HOME_ORIENTATION_EPSILON_DEG = 1e-6
ROTATION_POSITION_EPSILON_DEG = 0.05
DEFAULT_STAGE_RETRACT_TARGET_MM = 3.0
DEFAULT_Z_RETRACT_TARGET_MM = 1.0
DEFAULT_ROTATION_STAGE_MAX_EXTENSION_MM = 5.0
DEFAULT_ROTATION_Z_MAX_EXTENSION_MM = 5.0
MAX_PICK_LIFT_MM = 30.0


class RotationHomeOrientation(Enum):
    FRONT_MODULE = "front_module"
    BACK_MODULE = "back_module"


def _create_motor_if_enabled(
    controller: Controller,
    motors: c.Motors,
    name: str,
    safety,
    logger: logging.Logger,
    gpio=None,
    home_limit_pin: Optional[str] = None,
) -> Optional[Motor]:
    try:
        cfg: MotorConfig = motors.by_name(name)
    except KeyError:
        logger.info("Motor '%s' not found in config; skipping", name)
        return None

    if cfg.enabled is False:
        logger.info("Motor '%s' disabled via config; skipping", name)
        return None

    home_limit_input = None
    if home_limit_pin is not None and cfg.limit_switches_enabled is False:
        logger.info(
            "Motor '%s' limit switches disabled via config; skipping home limit switch '%s'",
            name,
            home_limit_pin,
        )
    elif gpio is not None and home_limit_pin is not None and gpio.has_pin(home_limit_pin):
        home_limit_input = gpio.input(home_limit_pin)

    return Motor(controller, cfg, safety, home_limit_input=home_limit_input)


def _limit_switches_enabled(motors: c.Motors, name: str) -> bool:
    """Only false when this machine's config says the axis has no limit switches."""
    try:
        return motors.by_name(name).limit_switches_enabled is not False
    except KeyError:
        return True


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
        return RotationHomeOrientation.FRONT_MODULE

    return RotationHomeOrientation.BACK_MODULE


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
        gpio=None,
    ):
        self._motorcontroller = motorcontroller
        self._lasercontroller = lasercontroller
        self._params = params
        self._guard_paused = False
        self._logger = logging.getLogger(__name__)
        self._init_reporter: Optional[Callable[[str, str, str, Optional[str]], None]] = None

        motors = self._params.motors

        self._mx: Optional[Motor] = _create_motor_if_enabled(
            motorcontroller, motors, "x_axis", safety, self._logger, gpio, home_limit_pin="x_lim_min"
        )

        self._my: Optional[Motor] = _create_motor_if_enabled(
            motorcontroller, motors, "y_axis", safety, self._logger, gpio, home_limit_pin="y_lim_min"
        )

        self._mg: Optional[Motor] = _create_motor_if_enabled(
            motorcontroller, motors, "z_axis", safety, self._logger, gpio, home_limit_pin="z_lim_min"
        )

        self._ms: Optional[Motor] = _create_motor_if_enabled(
            motorcontroller, motors, "stage", safety, self._logger, gpio, home_limit_pin="s_lim_min"
        )

        self._mr: Optional[Motor] = _create_motor_if_enabled(
            motorcontroller, motors, "rotate", safety, self._logger, gpio, home_limit_pin="r_lim_min"
        )

        self._gpio = gpio
        # Unfitted clearance inputs float, so reading them on a machine without
        # the switches would report every rotation as unsafe.
        if _limit_switches_enabled(motors, "x_axis") is False:
            self._x_lim_minunsafe_input = None
            self._x_lim_maxunsafe_input = None
            self._logger.info(
                "Motor 'x_axis' limit switches disabled via config; skipping rotation clearance "
                "limit switches 'x_lim_minunsafe' and 'x_lim_maxunsafe'"
            )
        else:
            self._x_lim_minunsafe_input = (
                gpio.input("x_lim_minunsafe") if gpio is not None and gpio.has_pin("x_lim_minunsafe") else None
            )
            self._x_lim_maxunsafe_input = (
                gpio.input("x_lim_maxunsafe") if gpio is not None and gpio.has_pin("x_lim_maxunsafe") else None
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

    def _get_float_param(self, param: str, default: float) -> float:
        """Read a validated tool parameter while supporting lightweight test doubles."""
        tool_params = getattr(getattr(self, "_params", None), "parameters", None)
        return float(getattr(tool_params, param, default))

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

    async def pick_lift_place(
        self,
        coordinate: c.AxisCoordinate,
        lift_mm: float,
        hold_seconds: float,
    ) -> None:
        if not 0 < lift_mm <= MAX_PICK_LIFT_MM:
            raise GripperError(f"Lift must be greater than zero and at most {MAX_PICK_LIFT_MM:g} mm")

        try:
            if self._my is None or self._ms is None:
                raise MotorError("Y and stage motors required for pick lift/place")

            target_position: Literal["front", "back"] = "front" if coordinate.s == 0 else "back"

            await self._gs.safe_open()
            await self.move(coordinate)
            await self.rotate_to_position(target_position)
            await self.extend_empty(coordinate)
            await self._gs.open()
            original_stage_mm = await self._ms.get_position()
            await self._ms.move_to_position(0.0)
            await self._my.move_to_position(coordinate.y + lift_mm)
            await asyncio.sleep(hold_seconds)
            await self._my.move_to_position(coordinate.y)
            await self._ms.move_to_position(original_stage_mm)
            await self._gs.close()
            await self.retract_empty()

        except Exception as e:
            await self._handle_fault(e)
            if isinstance(e, MovementError):
                raise
            raise GripperError(f"Pick lift/place operation failed: {str(e)}") from e

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

        async def stop_gantry_axes() -> list[str]:
            async def stop_motor(motor: Motor) -> None:
                cleanup_method = getattr(motor, "_stop_and_clear_motion", None)
                if cleanup_method is not None:
                    await cleanup_method()
                    return

                stop_method = getattr(motor, "stop", None)
                if stop_method is not None:
                    await stop_method()

            stop_results = await asyncio.gather(
                stop_motor(self._mx),
                stop_motor(self._my),
                return_exceptions=True,
            )
            return [
                f"{axis}={result}"
                for axis, result in zip(("x_axis", "y_axis"), stop_results)
                if isinstance(result, Exception)
            ]

        async def complete_gantry_cleanup() -> tuple[list[str], asyncio.CancelledError | None]:
            cleanup_task = asyncio.create_task(stop_gantry_axes())
            cancellation: asyncio.CancelledError | None = None

            while not cleanup_task.done():
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError as exc:
                    cancellation = exc

            return cleanup_task.result(), cancellation

        def attach_stop_errors(error: BaseException, stop_errors: list[str]) -> None:
            if not stop_errors:
                return

            fault_context = getattr(error, "fault_context", {})
            if not isinstance(fault_context, dict):
                fault_context = {}
            fault_context["gantry_stop_errors"] = stop_errors
            setattr(error, "fault_context", fault_context)

        try:
            await self._ensure_retracted()
            await self._my.move_to_position_no_wait(coordinate.y)
            await self._mx.move_to_position_no_wait(coordinate.x)
            await self.gantry_wait()

        except asyncio.CancelledError as e:
            stop_errors, cancellation = await complete_gantry_cleanup()
            attach_stop_errors(e, stop_errors)
            if cancellation is not None:
                attach_stop_errors(cancellation, stop_errors)
                raise cancellation
            raise
        except Exception as e:
            stop_errors, cancellation = await complete_gantry_cleanup()
            attach_stop_errors(e, stop_errors)
            if cancellation is not None:
                if hasattr(cancellation, "add_note"):
                    cancellation.add_note(f"Gantry movement error interrupted by cancellation: {e!r}")
                if hasattr(e, "fault_context"):
                    setattr(cancellation, "fault_context", getattr(e, "fault_context"))
                raise cancellation from e
            await self._handle_fault(e, phase="gantry.move", coordinate=coordinate)
            err = MovementError(f"Gantry movement failed: {str(e)}")
            if hasattr(e, "fault_context"):
                setattr(err, "fault_context", getattr(e, "fault_context"))
            raise err from e

    async def gantry_wait(
        self,
        timeout_seconds: float = GANTRY_OUTER_TIMEOUT,
        should_abort: Callable[[], bool] | None = None,
    ) -> None:
        if self._mx is None or self._my is None:
            raise MotorError("Gantry motors (x_axis, y_axis) required for gantry wait")

        start_time = time.monotonic()
        terminal_error_states = {MotorStatus.ERROR, MotorStatus.FAULT, MotorStatus.TIMEOUT}
        axis_statuses: dict[str, str] = {}

        try:
            remaining_timeout = max(0.1, min(GANTRY_WAIT_TIMEOUT, timeout_seconds))
            missing_pending_marker = object()
            commanded_motors = {
                axis: motor
                for axis, motor in (("x_axis", self._mx), ("y_axis", self._my))
                if getattr(motor, "_pending_motion", missing_pending_marker) is not None
            }

            if commanded_motors == {}:
                self._logger.info(
                    "Gantry wait skipped because no axes were commanded",
                    extra={"elapsed_s": time.monotonic() - start_time},
                )
                return

            tasks = {
                asyncio.create_task(
                    motor.wait_for_move(timeout=remaining_timeout, should_abort=should_abort)
                ): (axis, motor)
                for axis, motor in commanded_motors.items()
            }
            statuses: dict[str, MotorStatus] = {}

            def build_axis_statuses() -> dict[str, str]:
                return {name: item.name for name, item in statuses.items()}

            async def stop_commanded() -> list[str]:
                cleanup_tasks = []
                cleanup_axes = []

                for axis, motor in commanded_motors.items():
                    cleanup_method = getattr(motor, "_stop_and_clear_motion", None)

                    if cleanup_method is None:
                        async def cleanup_motor(item: Motor) -> None:
                            stop_method = getattr(item, "stop", None)
                            if stop_method is not None:
                                await stop_method()
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
                        statuses.setdefault(axis, MotorStatus.ERROR)
                        stop_errors.append(f"{axis}={str(result)}")
                        continue

                    statuses.setdefault(axis, MotorStatus.STOPPED)

                return stop_errors

            async def stop_all_and_drain(
                pending_tasks: set[asyncio.Task[MotorStatus]],
            ) -> list[str]:
                nonlocal axis_statuses

                stop_errors = await stop_commanded()
                for pending_task in pending_tasks:
                    pending_task.cancel()
                await asyncio.gather(*pending_tasks, return_exceptions=True)
                axis_statuses = build_axis_statuses()
                return stop_errors

            try:
                while tasks:
                    remaining_timeout = timeout_seconds - (time.monotonic() - start_time)

                    if remaining_timeout <= 0:
                        stop_errors = await stop_all_and_drain(set(tasks))
                        if stop_errors:
                            self._logger.error("Failed to stop gantry after timeout: %s", ", ".join(stop_errors))
                        raise MotorWaitTimeoutError(f"Gantry wait exceeded {timeout_seconds}s timeout")

                    done, pending = await asyncio.wait(
                        set(tasks),
                        timeout=remaining_timeout,
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    if not done:
                        stop_errors = await stop_all_and_drain(pending)
                        if stop_errors:
                            self._logger.error("Failed to stop gantry after timeout: %s", ", ".join(stop_errors))
                        raise MotorWaitTimeoutError(f"Gantry wait exceeded {timeout_seconds}s timeout")

                    for task in done:
                        axis, _motor = tasks.pop(task)

                        if task.cancelled() is True:
                            await stop_all_and_drain(pending)
                            raise asyncio.CancelledError()

                        exc = task.exception()
                        if exc is not None:
                            statuses[axis] = MotorStatus.ERROR
                            stop_errors = await stop_all_and_drain(pending)
                            if stop_errors:
                                fault_context = getattr(exc, "fault_context", {})
                                if not isinstance(fault_context, dict):
                                    fault_context = {}
                                fault_context["gantry_stop_errors"] = stop_errors
                                setattr(exc, "fault_context", fault_context)
                            raise exc

                        statuses[axis] = task.result()

                    if any(status in terminal_error_states for status in statuses.values()):
                        stop_errors = await stop_all_and_drain(pending)
                        axis_statuses = build_axis_statuses()
                        error = MovementError(
                            "Gantry motor error during wait: "
                            f"x={axis_statuses.get('x_axis', 'UNKNOWN')}, "
                            f"y={axis_statuses.get('y_axis', 'UNKNOWN')}"
                        )
                        if stop_errors:
                            setattr(error, "fault_context", {"gantry_stop_errors": stop_errors})
                        raise error

                    if any(status != MotorStatus.STOPPED for status in statuses.values()):
                        stop_errors = await stop_all_and_drain(pending)
                        axis_statuses = build_axis_statuses()
                        error = MovementError(
                            "Gantry movement ended unexpectedly: "
                            f"x={axis_statuses.get('x_axis', 'UNKNOWN')}, "
                            f"y={axis_statuses.get('y_axis', 'UNKNOWN')}"
                        )
                        if stop_errors:
                            setattr(error, "fault_context", {"gantry_stop_errors": stop_errors})
                        raise error
            except asyncio.CancelledError:
                await asyncio.shield(stop_all_and_drain(set(tasks)))
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

            if orientation == RotationHomeOrientation.BACK_MODULE:
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
        """Prepare the stage and verify both tool axes are safe for rotation.

        Some machines require a minimum stage extension while rotating. All
        others use the configured maximum stage and Z extensions as a
        retract-before-rotate interlock.
        """
        did_extend = False
        stage_position: float | None = None
        stage_extension_enabled = False

        if self._ms is not None:
            stage_extension_enabled = getattr(
                self._ms._params,
                "rotation_safe_stage_extension_enabled",
                False,
            )
            stage_position = float(await self._ms.get_position())

            if stage_extension_enabled is True:
                safe_extension_mm = float(self._ms._params.rotation_safe_stage_extension_mm)
                stage_limit = float(self._ms._params.lim)

                if safe_extension_mm > stage_limit:
                    raise ConfigError(
                        "rotation_safe_stage_extension_mm exceeds stage limit "
                        f"({safe_extension_mm} > {stage_limit})"
                    )

                if stage_position < safe_extension_mm:
                    await self._ms.move_to_position(safe_extension_mm)
                    did_extend = True
                    stage_position = float(await self._ms.get_position())

        await self._assert_rotation_clearance(
            stage_position=stage_position,
            stage_extension_enabled=stage_extension_enabled,
        )
        return did_extend

    async def _assert_rotation_clearance(
        self,
        *,
        stage_position: float | None = None,
        stage_extension_enabled: bool | None = None,
    ) -> None:
        unsafe: list[str] = []

        if self._ms is not None:
            if stage_position is None:
                stage_position = float(await self._ms.get_position())
            if stage_extension_enabled is None:
                stage_extension_enabled = getattr(
                    self._ms._params,
                    "rotation_safe_stage_extension_enabled",
                    False,
                )

            if stage_extension_enabled is True:
                minimum_stage_mm = float(self._ms._params.rotation_safe_stage_extension_mm)
                if stage_position < minimum_stage_mm:
                    unsafe.append(
                        f"stage={stage_position:.3f}mm is below required "
                        f"{minimum_stage_mm:.3f}mm"
                    )
            else:
                maximum_stage_mm = self._get_float_param(
                    "ROTATION_STAGE_MAX_EXTENSION_MM",
                    DEFAULT_ROTATION_STAGE_MAX_EXTENSION_MM,
                )
                if stage_position > maximum_stage_mm:
                    unsafe.append(
                        f"stage={stage_position:.3f}mm exceeds maximum "
                        f"{maximum_stage_mm:.3f}mm"
                    )

        z_motor = getattr(self, "_mg", None)
        if z_motor is not None:
            z_position = float(await z_motor.get_position())
            maximum_z_mm = self._get_float_param(
                "ROTATION_Z_MAX_EXTENSION_MM",
                DEFAULT_ROTATION_Z_MAX_EXTENSION_MM,
            )
            if z_position > maximum_z_mm:
                unsafe.append(
                    f"z_axis={z_position:.3f}mm exceeds maximum {maximum_z_mm:.3f}mm"
                )

        x_lim_minunsafe_input = getattr(self, "_x_lim_minunsafe_input", None)
        if x_lim_minunsafe_input is not None and x_lim_minunsafe_input.read() is not True:
            unsafe.append("x_lim_minunsafe is not engaged")

        x_lim_maxunsafe_input = getattr(self, "_x_lim_maxunsafe_input", None)
        if x_lim_maxunsafe_input is not None and x_lim_maxunsafe_input.read() is not True:
            unsafe.append("x_lim_maxunsafe is not engaged")

        if unsafe:
            raise MovementError("Unsafe tool clearance for rotation: " + "; ".join(unsafe))

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
            await self.tool_wait()
        except Exception as e:
            await self._handle_fault(e)

            if isinstance(e, ConfigError):
                raise

            raise MovementError(f"Tool empty extension failed: {str(e)}") from e

    async def retract(self) -> None:
        if self._mg is None or self._ms is None:
            raise MotorError("Tool motors (z_axis, stage) required for retraction")

        try:
            await self._retract_to_configured_positions()

        except Exception as e:
            await self._handle_fault(e)

            raise MovementError(f"Tool retraction failed: {str(e)}") from e

    async def retract_empty(self) -> None:
        if self._mg is None or self._ms is None:
            raise MotorError("Tool motors (z_axis, stage) required for empty retraction")

        try:
            await self._retract_to_configured_positions()

        except Exception as e:
            await self._handle_fault(e)
            raise MovementError(f"Tool empty retraction failed: {str(e)}") from e

    async def _retract_to_configured_positions(self) -> None:
        if self._mg is None or self._ms is None:
            raise MotorError("Tool motors (z_axis, stage) required for retraction")

        grip_rpm = await self.get_param("Z_MOVE_RPM")
        grip_accel = await self.get_param("Z_MOVE_ACCEL")

        if grip_rpm is None or grip_accel is None:
            raise ConfigError("Missing gripper move speed")

        await self._move_axis_to_retracted_position(
            self._mg,
            axis_name="z_axis",
            target_mm=self._get_float_param(
                "Z_RETRACT_POSITION_MM",
                DEFAULT_Z_RETRACT_TARGET_MM,
            ),
            maximum_mm=self._get_float_param(
                "ROTATION_Z_MAX_EXTENSION_MM",
                DEFAULT_ROTATION_Z_MAX_EXTENSION_MM,
            ),
            rpm=grip_rpm,
            accel=grip_accel,
        )
        await self._move_axis_to_retracted_position(
            self._ms,
            axis_name="stage",
            target_mm=self._get_float_param(
                "STAGE_RETRACT_POSITION_MM",
                DEFAULT_STAGE_RETRACT_TARGET_MM,
            ),
            maximum_mm=self._get_float_param(
                "ROTATION_STAGE_MAX_EXTENSION_MM",
                DEFAULT_ROTATION_STAGE_MAX_EXTENSION_MM,
            ),
        )
        await self._ms.idle()

    async def tool_wait(
        self,
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
                    should_abort=should_abort,
                )

                ms_f = await self._ms.wait_for_move(
                    timeout=TOOL_WAIT_TIMEOUT,
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

    async def _move_axis_to_retracted_position(
        self,
        motor: Motor,
        *,
        axis_name: str,
        target_mm: float,
        maximum_mm: float,
        rpm: int | None = None,
        accel: int | None = None,
    ) -> None:
        move_error: BaseException | None = None
        try:
            if rpm is None and accel is None:
                await motor.move_to_position(target_mm)
            else:
                await motor.move_to_position(target_mm, rpm=rpm, accel=accel)
        except Exception as error:
            move_error = error

        if move_error is None:
            position_mm = float(await motor.get_position())
            if position_mm <= maximum_mm:
                return
            move_error = MovementError(
                f"{axis_name} retract stopped outside rotation clearance "
                f"({position_mm:.3f} > {maximum_mm:.3f})"
            )

        self._logger.warning(
            "%s retract failed clearance verification; homing as recovery",
            axis_name,
            extra={"error": str(move_error)},
        )

        try:
            await motor.clear_alarm()
        except Exception as clear_error:
            self._logger.warning(
                "Failed to clear %s alarm before retract recovery home",
                axis_name,
                extra={"error": str(clear_error)},
            )

        await motor.home()
        position_mm = float(await motor.get_position())

        if position_mm > maximum_mm:
            raise MovementError(
                f"{axis_name} retract recovery home left axis outside rotation clearance "
                f"({position_mm:.3f} > {maximum_mm:.3f})"
            ) from move_error

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

    async def _home_sequence(
        self,
        isolate: bool = False,
        failed_config_motors: Optional[List[Motor]] = None,
    ) -> None:
        failed_config_motors = failed_config_motors or []
        failed_home_motors: List[Motor] = []

        for motor in (self._mg, self._ms):
            if isolate is True:
                if await self._home_step_isolated(motor, failed_config_motors) is False and motor is not None:
                    failed_home_motors.append(motor)
            elif motor is not None:
                await motor.home()

        if isolate is True:
            await self._home_rotate_isolated(failed_config_motors, failed_home_motors)
        elif self._mr is not None:
            await self._home_rotate_sequence()

        for motor in (self._mx, self._my):
            if isolate is True:
                await self._home_step_isolated(motor, failed_config_motors)
            elif motor is not None:
                await motor.home()

    def _did_motor_configure_fail(self, motor: Optional[Motor], failed_config_motors: List[Motor]) -> bool:
        if motor is None:
            return False
        return any(failed_motor is motor for failed_motor in failed_config_motors)

    async def _home_step_isolated(
        self,
        motor: Optional[Motor],
        failed_config_motors: List[Motor],
    ) -> bool:
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

    async def home(
        self,
        isolate: bool = False,
        failed_config_motors: Optional[List[Motor]] = None,
    ) -> None:
        if isolate is False:
            try:
                await self._home_sequence()
            except Exception as e:
                await self._handle_fault(e)

                raise MotorError(f"Failed to home tool: {str(e)}") from e
            return

        failed_config_motors = failed_config_motors or []

        await self._home_sequence(
            isolate=True,
            failed_config_motors=failed_config_motors,
        )
