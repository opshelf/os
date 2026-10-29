import asyncio
import functools
import logging
import time

import openshelf_os.common as c

from enum import Enum
from typing import Optional, Literal, List, Any, Awaitable, Callable, Sequence
from openshelf_os.common import Motor as MotorConfig
from hardware.controller import SharedModbusController as Controller
from hardware.laser import LaserSensor
from hardware.motor import Motor, MotorStatus, MOTOR_WAIT_TIMEOUT, SETTLED_ON_TARGET_TOLERANCE_MM
from hardware.servo import GripState, Servo
from openshelf_os.common import load_config_file
from openshelf_os.exceptions import (
    MotorError, ConfigError,
    GripperError, GripVerificationError, MovementError, MotorWaitTimeoutError,
    GRIP_DROPPED_CODE, GRIP_PICK_FAILED_CODE
)


GANTRY_WAIT_TIMEOUT = MOTOR_WAIT_TIMEOUT * 2
TOOL_WAIT_TIMEOUT = MOTOR_WAIT_TIMEOUT * 2
GANTRY_OUTER_TIMEOUT = 120.0
TOOL_OUTER_TIMEOUT = 60.0
ROTATION_SAFE_X_MARGIN_MM = 200.0
# The clearance move lands on the window edge; encoder reads settle a few hundredths either side.
ROTATION_SAFE_X_TOLERANCE_MM = 1.0
ROTATION_HOME_ORIENTATION_EPSILON_DEG = 1e-6
ROTATION_POSITION_EPSILON_DEG = 0.05
DEFAULT_STAGE_RETRACT_TARGET_MM = 3.0
DEFAULT_Z_RETRACT_TARGET_MM = 1.0
DEFAULT_ROTATION_STAGE_MAX_EXTENSION_MM = 5.0
DEFAULT_ROTATION_Z_MAX_EXTENSION_MM = 5.0
MAX_PICK_LIFT_MM = 30.0
# The tool's own axes. A gripper feedback fault leaves the gantry position valid, so a reset
# after one homes these, plus the IO box door when the tray was in the IO box.
TOOL_MOTION_AXES: tuple[str, ...] = ("z_axis", "stage", "rotate")
Y_STARTUP_HOME_CLEARANCE_MM = 10.0
Y_STARTUP_HOME_PEAK_CURRENT = 30


async def _finish_despite_cancel(work: Awaitable[Any]) -> tuple[Any, asyncio.CancelledError | None]:
    """Run cleanup to completion even if this task is cancelled meanwhile.

    Returns the cleanup result and the last cancellation seen, for the caller to re-raise.
    """
    task = asyncio.ensure_future(work)
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as cancelled:
            cancellation = cancelled
    return task.result(), cancellation


async def run_together(*steps: Awaitable[Any]) -> None:
    """Run independent motions at once; the first failure stops the rest before it propagates."""
    tasks = [asyncio.ensure_future(step) for step in steps]
    try:
        await asyncio.gather(*tasks)
    except BaseException as error:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # A sibling that failed while being stopped (e.g. its stop write) would otherwise
        # vanish behind the first error.
        for task in tasks:
            # gather(return_exceptions=True) swaps a cancelled task's exception for a fresh
            # CancelledError, dropping what the task attached; result() re-raises the original.
            try:
                result = task.exception() if not task.cancelled() else task.result()
            except asyncio.CancelledError as cancelled:
                result = cancelled
            if result is error:
                continue
            if isinstance(result, Exception):
                failure = repr(result)
            elif isinstance(result, asyncio.CancelledError):
                # A cancelled gantry move carries its stop failure as fault_context or a cause;
                # a plain cancellation carries neither.
                stop_errors = getattr(result, "fault_context", None)
                cause = result.__cause__
                if not stop_errors and not isinstance(cause, Exception):
                    continue
                failure = f"cancelled with {cause!r}" if isinstance(cause, Exception) else "cancelled"
                if stop_errors:
                    failure += f" {stop_errors!r}"
            else:
                continue
            logging.getLogger(__name__).warning(
                "Concurrent motion also failed while being stopped", exc_info=result
            )
            if hasattr(error, "add_note"):
                error.add_note(f"concurrent motion also failed: {failure}")
        raise


def _is_settled_at(motor: Motor, target_mm: float) -> bool:
    """A homed axis already within the drive's settle error of its target needs no move.

    The drives stop a few hundredths short of a commanded target, so re-commanding the
    same coordinate would otherwise run a full move (and, on Y, a brake release and park).
    """
    position = getattr(motor, "_cur_pos", None)
    if getattr(motor, "_is_homed", False) is not True or not isinstance(position, (int, float)):
        return False
    return abs(position - target_mm) <= SETTLED_ON_TARGET_TOLERANCE_MM


class RotationHomeOrientation(Enum):
    FRONT_MODULE = "front_module"
    BACK_MODULE = "back_module"


def require_grip(
    state: GripState,
    phase: str,
    *,
    is_enabled: bool = True,
    failure_code: str = GRIP_DROPPED_CODE,
) -> None:
    """Raise unless the gripper reports object contact with no drop.

    A machine with gripper.object_detection_enabled false passes is_enabled=False and
    skips the check. failure_code says where the tray was last held, so a pick that never
    lifted can be cancelled without changing inventory.
    """
    if is_enabled is False:
        return
    # Every reading, not only failures: tuning open_position needs the passing margins too.
    logging.getLogger(__name__).info(
        "grip_check", extra={"phase": phase, "gripped": state.gripped, "grip_state": str(state)}
    )
    if not state.gripped:
        raise GripVerificationError(f"No grip on {phase}: {state}", failure_code=failure_code)


def rewrap_gripper_fault(error: Exception, message: str) -> Exception:
    """Re-raise a gripper fault with context, keeping grip feedback apart from servo faults."""
    if isinstance(error, GripVerificationError):
        return GripVerificationError(f"{message}: {error}", failure_code=error.failure_code)
    return GripperError(f"{message}: {error}")


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
    # Set from gripper.object_detection_enabled in __init__.
    is_grip_detection_enabled: bool = True

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

        self._gs = Servo(self._motorcontroller, self._params.gripper.model_dump())
        self.is_grip_detection_enabled = self._params.gripper.object_detection_enabled
        if self.is_grip_detection_enabled is False:
            self._logger.info(
                "Gripper object detection disabled via config; picks and tray returns skip the grip check"
            )

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
                if await self._found_tray_on_tool() is True:
                    # Activating the gripper again may move its fingers and drop the tray
                    # somewhere in the machine, so it stays as found until the operator releases it.
                    home_on_init = False
                elif isolate is True:
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
                await self.home(
                    isolate=isolate,
                    failed_config_motors=failed_config_motors,
                    y_pre_home_clearance_mm=Y_STARTUP_HOME_CLEARANCE_MM,
                )
            else:
                self._mark_configured_motors_ready(failed_config_motors)
        except Exception as e:
            await self._handle_fault(e)

            raise MotorError(f"Tool configuration failed: {str(e)}") from e

    @property
    def is_tray_held(self) -> bool:
        """Holding state as last read or commanded, without touching the bus."""
        return self._gs.holding is True

    async def read_tray_held(self) -> bool:
        """Refresh the gripper's holding state from gOBJ and return it.

        The gripper is the source of truth. Without a usable reading (object detection
        disabled, a failed read, or a gripper not activated since it lost power) the last grip
        command stands, and with none since startup the tool counts as empty: there is no
        evidence either way, as before tracking existed.
        """
        if self._params.gripper.enabled is False:
            return False
        if self.is_grip_detection_enabled is True:
            try:
                state = await self._gs.read_state()
            except Exception as e:
                self._logger.warning("tray_on_tool gripper read failed; using the last grip command: %s", e)
            else:
                if state.activated is True:
                    self._gs.holding = state.holding
        return self._gs.holding is True

    async def _found_tray_on_tool(self) -> bool:
        is_held = await self.read_tray_held()
        if is_held is True:
            self._logger.warning("tray_on_tool at startup: gripper left unconfigured and homing skipped")
        return is_held

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
            require_grip(
                await self._gs.open(),
                "grip",
                is_enabled=self.is_grip_detection_enabled,
                failure_code=GRIP_PICK_FAILED_CODE,
            )
            await self.retract()
            require_grip(await self._gs.read_state(), "retract", is_enabled=self.is_grip_detection_enabled)

        except Exception as e:
            await self._handle_fault(e)
            if isinstance(e, MovementError):
                raise
            raise rewrap_gripper_fault(e, "Pick operation failed") from e

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
            require_grip(
                await self._gs.open(),
                "grip",
                is_enabled=self.is_grip_detection_enabled,
                failure_code=GRIP_PICK_FAILED_CODE,
            )
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
            raise rewrap_gripper_fault(e, "Pick lift/place operation failed") from e

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

    async def get_laser_distance(self, *, timeout_s: Optional[float] = None) -> Optional[float]:
        if self._laser_enabled is False or self._laser is None:
            return None

        if self._laser_ready is False:
            raise ConfigError("Laser is not initialized")

        if timeout_s is None:
            return await self._laser.read_distance_mm()

        return await self._laser.read_distance_mm(timeout_s=timeout_s)

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
            moving = [
                (axis, motor, target)
                for axis, motor, target in (
                    ("y_axis", self._my, coordinate.y),
                    ("x_axis", self._mx, coordinate.x),
                )
                if _is_settled_at(motor, target) is False
            ]
            # A hold already active is waited out here: the starts below run inside the gantry
            # deadline, which must not be spent on a hold that was there before the move began.
            held = {id(s): s for _, m, _ in moving if (s := getattr(m, "_safety", None)) is not None}
            for safety in held.values():
                await safety.wait_clear()
            # Started together, X is already moving while Y waits out its brake release, and
            # each axis is watched from its own start rather than once both have started.
            await self.gantry_wait(
                starts={axis: motor.move_to_position_no_wait(target) for axis, motor, target in moving}
            )

        except asyncio.CancelledError as e:
            stop_errors, cancellation = await _finish_despite_cancel(stop_gantry_axes())
            attach_stop_errors(e, stop_errors)
            if cancellation is not None:
                attach_stop_errors(cancellation, stop_errors)
                raise cancellation
            raise
        except Exception as e:
            stop_errors, cancellation = await _finish_despite_cancel(stop_gantry_axes())
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
        starts: dict[str, Awaitable[bool]] | None = None,
    ) -> None:
        """Wait for the commanded gantry axes, stopping both if either fails.

        `starts` maps an axis to the coroutine that starts its move; each axis is then
        watched as soon as its own start returns, and a failed start stops the other.
        """
        if self._mx is None or self._my is None:
            raise MotorError("Gantry motors (x_axis, y_axis) required for gantry wait")

        start_time = time.monotonic()
        terminal_error_states = {MotorStatus.ERROR, MotorStatus.FAULT, MotorStatus.TIMEOUT}
        axis_statuses: dict[str, str] = {}

        try:
            remaining_timeout = max(0.1, min(GANTRY_WAIT_TIMEOUT, timeout_seconds))
            missing_pending_marker = object()
            gantry_motors = {"x_axis": self._mx, "y_axis": self._my}
            if starts is not None:
                commanded_motors = {axis: gantry_motors[axis] for axis in starts}
            else:
                commanded_motors = {
                    axis: motor
                    for axis, motor in gantry_motors.items()
                    if getattr(motor, "_pending_motion", missing_pending_marker) is not None
                }

            if commanded_motors == {}:
                self._logger.info(
                    "Gantry wait skipped because no axes were commanded",
                    extra={"elapsed_s": time.monotonic() - start_time},
                )
                return

            wait_timeout = remaining_timeout

            async def start_then_wait(start: Awaitable[bool], motor: Motor) -> MotorStatus:
                if await start is False:
                    return MotorStatus.STOPPED
                return await motor.wait_for_move(timeout=wait_timeout, should_abort=should_abort)

            tasks = {
                asyncio.create_task(
                    start_then_wait(starts[axis], motor)
                    if starts is not None
                    else motor.wait_for_move(timeout=wait_timeout, should_abort=should_abort)
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
                            # The other axis may already be moving (a start can fail after
                            # its sibling triggered), so its stop finishes even if this
                            # task is cancelled meanwhile.
                            stop_errors, cancellation = await _finish_despite_cancel(
                                stop_all_and_drain(pending)
                            )
                            if stop_errors:
                                fault_context = getattr(exc, "fault_context", {})
                                if not isinstance(fault_context, dict):
                                    fault_context = {}
                                fault_context["gantry_stop_errors"] = stop_errors
                                setattr(exc, "fault_context", fault_context)
                            if cancellation is not None:
                                raise cancellation from exc
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

        # The tool only clears the gantry uprights while X is ROTATION_SAFE_X_MARGIN_MM from both
        # ends of its travel; ensure_safe_rotation_x_position moves it there before rotating.
        x_motor = getattr(self, "_mx", None)
        if x_motor is not None:
            if getattr(x_motor, "_is_homed", True) is False:
                unsafe.append("x_axis is not homed")
            else:
                x_position = float(await x_motor.get_position())
                window_min_mm = float(getattr(x_motor._params, "lower_lim", 0.0)) + ROTATION_SAFE_X_MARGIN_MM
                window_max_mm = float(x_motor._params.lim) - ROTATION_SAFE_X_MARGIN_MM

                if (
                    x_position < window_min_mm - ROTATION_SAFE_X_TOLERANCE_MM
                    or x_position > window_max_mm + ROTATION_SAFE_X_TOLERANCE_MM
                ):
                    unsafe.append(
                        f"x_axis={x_position:.3f}mm is outside the rotation window "
                        f"{window_min_mm:.1f}-{window_max_mm:.1f}mm"
                    )

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

        if getattr(x_motor, "_is_homed", True) is False:
            raise MotorError("X axis is not homed; home X before rotating")

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
            await run_together(
                self._ms.move_to_position(slide_ext),
                self._mg.move_to_position(grip_ext, rpm=grip_rpm, accel=grip_accel),
            )

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
            await run_together(
                self._ms.move_to_position(slide_ext),
                self._mg.move_to_position(grip_ext, rpm=grip_rpm, accel=grip_accel),
            )
            await self._ms.move_to_position(slide_ext + stage_nudge)
        except Exception as e:
            await self._handle_fault(e)

            if isinstance(e, ConfigError):
                raise

            raise MovementError(f"Tool IO extension failed: {str(e)}") from e

    async def extend_IO_multi_item(self) -> None:
        """Extend to the multi-item tray pose in the IO box; trays need no stage nudge."""
        if self._mg is None or self._ms is None:
            raise MotorError("Tool motors (z_axis, stage) required for IO extension")

        slide_ext = await self.get_param("STAGE_EXTENSION_IO_BOX_MULTI_ITEM")
        grip_ext = await self.get_param("Z_EXTENSION_IO_MULTI_ITEM")
        grip_rpm = await self.get_param("Z_MOVE_RPM")
        grip_accel = await self.get_param("Z_MOVE_ACCEL")

        if slide_ext is None or grip_ext is None or grip_rpm is None or grip_accel is None:
            raise ConfigError("Missing multi-item IO box extension parameters")

        self._warn_stage_target(slide_ext, "STAGE_EXTENSION_IO_BOX_MULTI_ITEM")

        try:
            await self._ms.ready()
            await run_together(
                self._ms.move_to_position(slide_ext),
                self._mg.move_to_position(grip_ext, rpm=grip_rpm, accel=grip_accel),
            )
        except Exception as e:
            await self._handle_fault(e)

            if isinstance(e, ConfigError):
                raise

            raise MovementError(f"Tool multi-item IO extension failed: {str(e)}") from e

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

        await run_together(
            self._move_axis_to_retracted_position(
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
            ),
            self._move_axis_to_retracted_position(
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
            ),
        )
        await self._ms.idle()

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
            # The clearance move below is in X coordinates; after a power cycle the drive
            # reads 0 wherever the carriage stands, so X must be homed before it moves.
            if getattr(self._mx, "_is_homed", True) is False:
                await self._mx.home()

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
        y_pre_home_clearance_mm: Optional[float] = None,
        axes: Optional[Sequence[str]] = None,
    ) -> None:
        failed_config_motors = failed_config_motors or []
        failed_home_motors: List[Motor] = []

        def should_home(axis: str) -> bool:
            return axes is None or axis in axes

        # X homes twice around rotate. The first home only gives the rotate clearance move real
        # coordinates (after a power loss the drive reads 0 wherever the carriage stands), and
        # with the tool at an unknown angle it can stop on a gantry upright short of the hard
        # stop. The second runs with the tool straight and sets the zero every later move uses.
        for motor, axis in ((self._mg, "z_axis"), (self._ms, "stage"), (self._mx, "x_axis")):
            if motor is None or should_home(axis) is False:
                continue
            if isolate is True:
                if await self._home_step_isolated(motor, failed_config_motors) is False:
                    failed_home_motors.append(motor)
            else:
                await motor.home()

        did_home_rotate = False

        if should_home("rotate"):
            if isolate is True:
                did_home_rotate = await self._home_rotate_isolated(failed_config_motors, failed_home_motors)
            elif self._mr is not None:
                await self._home_rotate_sequence()
                did_home_rotate = True

        if did_home_rotate is True and self._mx is not None and should_home("x_axis"):
            if isolate is True:
                await self._home_step_isolated(self._mx, failed_config_motors)
            else:
                await self._mx.home()

        if should_home("y_axis") and self._my is not None:
            if isolate is True:
                await self._home_step_isolated(
                    self._my,
                    failed_config_motors,
                    pre_home_clearance_mm=y_pre_home_clearance_mm,
                )
            elif y_pre_home_clearance_mm is None:
                await self._my.home()
            else:
                await self._my.home(
                    pre_home_clearance_mm=y_pre_home_clearance_mm,
                    skip_pre_home_approach=True,
                    homing_peak_current=Y_STARTUP_HOME_PEAK_CURRENT,
                )

    def _did_motor_configure_fail(self, motor: Optional[Motor], failed_config_motors: List[Motor]) -> bool:
        if motor is None:
            return False
        return any(failed_motor is motor for failed_motor in failed_config_motors)

    async def _home_step_isolated(
        self,
        motor: Optional[Motor],
        failed_config_motors: List[Motor],
        pre_home_clearance_mm: Optional[float] = None,
    ) -> bool:
        if motor is None:
            return True
        if self._did_motor_configure_fail(motor, failed_config_motors) is True:
            return True
        try:
            if pre_home_clearance_mm is None:
                await motor.home()
            else:
                await motor.home(
                    pre_home_clearance_mm=pre_home_clearance_mm,
                    skip_pre_home_approach=True,
                    homing_peak_current=Y_STARTUP_HOME_PEAK_CURRENT,
                )
            return True
        except Exception as e:
            self._logger.warning("init home step failed: %s", e)
            return False

    async def _home_rotate_isolated(
        self,
        failed_config_motors: List[Motor],
        failed_home_motors: Optional[List[Motor]] = None,
    ) -> bool:
        if self._mr is None:
            return False
        if self._did_motor_configure_fail(self._mr, failed_config_motors) is True:
            return False
        failed_home_motors = failed_home_motors or []
        if any(
            self._did_motor_configure_fail(motor, failed_config_motors) is True
            or any(failed_home_motor is motor for failed_home_motor in failed_home_motors)
            for motor in (self._mx, self._ms)
        ):
            self._emit_init("rotate", "failed", "home", "dependency failed")
            return False
        try:
            await self._home_rotate_sequence()
            return True
        except Exception as e:
            self._emit_init("rotate", "failed", "home", str(e))
            self._logger.warning("init rotate home failed: %s", e)
            return False

    async def home(
        self,
        isolate: bool = False,
        failed_config_motors: Optional[List[Motor]] = None,
        y_pre_home_clearance_mm: Optional[float] = None,
        axes: Optional[Sequence[str]] = None,
    ) -> None:
        if isolate is False:
            try:
                await self._home_sequence(
                    y_pre_home_clearance_mm=y_pre_home_clearance_mm,
                    axes=axes,
                )
            except Exception as e:
                await self._handle_fault(e)

                raise MotorError(f"Failed to home tool: {str(e)}") from e
            return

        failed_config_motors = failed_config_motors or []

        await self._home_sequence(
            isolate=True,
            failed_config_motors=failed_config_motors,
            y_pre_home_clearance_mm=y_pre_home_clearance_mm,
        )
