import asyncio
import json as _json
import logging
import sentry_sdk

from pathlib import Path as _Path
from typing import Any, Callable, Dict, Iterable, NamedTuple, Optional, List, TYPE_CHECKING

from hardware.controller import SharedModbusController as Controller
from hardware.curtain import MotionSafetyInterlock
from hardware.motor import MotorStatus
from openshelf_os.cabinet import Cabinet
from openshelf_os.common import AxisCoordinate, ConfigError, load_config_file, CONFIG_PATH, resolve_grid_path
from openshelf_os.demo_side_rename import DEMO_SIDE_RENAME_BACKUP_SUFFIX, migrate_demo_side_rename
from openshelf_os.config.models import compare_gpio_pins_to_defaults
from openshelf_os.serial_ports import (
    DEFAULT_LASER_PORT,
    DEFAULT_MOTOR_PORT,
    DEFAULT_SCALE_PORT,
    LASER_PORT_CANDIDATES,
    MOTOR_PORT_CANDIDATES,
    SCALE_PORT_CANDIDATES,
)
from openshelf_os.exceptions import ConfigError as CabinetConfigError, GripperError, MotorError, MovementError, ToolError
from openshelf_os.iobox import IOBox
from openshelf_os.laser_calibration.scan import (
    ScanAnalysisResult, ScanBounds, analyze_scan_capture,
    build_axis_positions, build_shelves_from_scan, create_scan_capture,
)
from openshelf_os.operating_mode import should_enable_demo_features
from openshelf_os.robot.commands import (
    RobotCommands, StopCommand, InitializeCommand, IdleCommand, HomeCommand,
    MoveCommand, RetrieveItemCommand, StoreItemCommand, DeliverTrayCommand, ReturnTrayCommand,
)
from openshelf_os.robot.status import (
    RobotResponse, RobotCommandSuccessful, RobotCommandError,
    RobotStatus, GantryStatus, ToolAxisStatus, RobotState, AxisTelemetry, CabinetDoorState,
)
from openshelf_os.robot.weight_guard import WeightChangeGuard
from openshelf_os.storage import (
    StorageGrid, Dimensions, StorageModuleSide, CellItem, StorageModuleCellLocation, CellStatus,
    StorageValidationError,
)
from openshelf_os.tool import Tool, require_grip

_GpioManager: Any
try:
    from hardware.gpio import GpioManager as _GpioManager
except Exception:
    _GpioManager = None

if TYPE_CHECKING:
    from hardware.motor import Motor


logger = logging.getLogger(__name__)

# (component, phase, detail, reason)
InitProgressReporter = Callable[[str, str, str, Optional[str]], None]

_INIT_MOTOR_COMPONENTS = ["x_axis", "y_axis", "z_axis", "stage", "rotate", "door"]

class PowerRail(NamedTuple):
    supplies: str
    #: Motors that lose their incremental encoder reference with the rail.
    axes: tuple[str, ...]
    #: Whether the gripper servo drops too, so it must reconfigure on next use.
    gripper: bool = False


# The four switched power rails. Asserting a pin CUTS that rail; clearing it --
# the default state -- applies power.
POWER_RAILS: Dict[str, PowerRail] = {
    "pwr_out0": PowerRail(
        "tool drives and gripper",
        ("x_axis", "y_axis", "z_axis", "stage", "rotate"),
        gripper=True,
    ),
    "pwr_out1": PowerRail("IO box", ("door",)),
    "pwr_out2": PowerRail("peripherals", ()),
    "pwr_out3": PowerRail("aux", ()),
}
POWER_CYCLE_HOLD_S = 3.0
POWER_CYCLE_BOOT_TIMEOUT_S = 20.0
POWER_CYCLE_BOOT_POLL_S = 0.25


class Robot:
    # ------------------------------------------------------------------ #
    #  Lifecycle
    # ------------------------------------------------------------------ #

    def __init__(self) -> None:
        self._config = load_config_file(CONFIG_PATH)
        self._bus_settings = self._prepare_bus_settings()
        self._logger = logging.getLogger(__name__).getChild("Robot")

        self._motorbus = self._create_controller_for_bus("motor")
        self._scalebus = self._create_controller_for_bus("scale")
        self._laserbus = self._create_controller_for_bus("laser")

        tool_params = self._config.get("robot.tool", {})
        io_params = self._config.get("robot.io", {})
        gpio_config = getattr(self._config.robot, "gpio", None)
        self._gpio = _GpioManager.from_config(gpio_config) if _GpioManager is not None else None
        self._warn_on_gpio_pin_drift(gpio_config)

        self._motion_safety = MotionSafetyInterlock()
        self._tool = Tool(
            motorcontroller=self._motorbus,
            lasercontroller=self._laserbus,
            params=tool_params,
            safety=self._motion_safety,
            gpio=self._gpio,
        )
        self._io = IOBox(
            motorcontroller=self._motorbus,
            scalecontroller=self._scalebus,
            params=io_params,
            safety=self._motion_safety,
            gpio=self._gpio,
        )
        self._cabinet = Cabinet(gpio=self._gpio)

        self._grid_path = resolve_grid_path(self._config, CONFIG_PATH)
        self._reserved_cell_locations: set[tuple[str, int, int]] = {
            (entry.home_location.side, entry.home_location.shelf_idx, entry.home_location.cell_idx)
            for entry in self._config.storage.bins
        }

        try:
            self._grid = StorageGrid.load_from_file(self._grid_path)
        except StorageValidationError as e:
            self._logger.warning("storage_load_failed: %s - %s", self._grid_path, str(e))
            self._grid = StorageGrid()

        self._init_reporter: Optional[InitProgressReporter] = None

        self._state: RobotState = RobotState(current_cell=None)
        self._operation_context: dict[str, Any] = {}
        self._operation_context_listener: Callable[[dict[str, Any] | None], None] | None = None
        self._last_fault_context: dict[str, Any] | None = None
        self._motor_time_accumulator: float = 0.0

        self._weight_guard = self._build_weight_guard()

        self._demo_cell_map: dict[str, StorageModuleCellLocation] | None = None
        self._demo_nudge_map: dict[tuple[str, int, int], float] = {}
        if should_enable_demo_features() is True:
            self._demo_cell_map = self._load_demo_cell_map(self._config.demo.cell_map_path)

    def _warn_on_gpio_pin_drift(self, gpio_config: Any) -> None:
        """Flag pins the defaults define but this config lacks, and vice versa.

        A config that declares robot.gpio.pins replaces the defaults outright,
        so a renamed default goes missing here rather than failing loudly.
        """
        pins = getattr(gpio_config, "pins", None)
        if pins is None:
            return

        missing, unrecognized = compare_gpio_pins_to_defaults(pins)

        if missing:
            self._logger.warning(
                "config gpio pins are missing defaults; features using them are unavailable",
                extra={"missing_pins": missing},
            )
        if unrecognized:
            self._logger.warning(
                "config gpio pins are not in the defaults; check for a stale or renamed key",
                extra={"unrecognized_pins": unrecognized},
            )

    def _emit_init(self, component: str, phase: str, detail: str = "", reason: Optional[str] = None) -> None:
        reporter = getattr(self, "_init_reporter", None)
        if reporter is None:
            return
        try:
            reporter(component, phase, detail, reason)
        except Exception:
            pass

    def set_init_progress_reporter(self, reporter: Optional[InitProgressReporter]) -> None:
        self._init_reporter = reporter
        self._tool._init_reporter = reporter
        self._io._init_reporter = reporter

        def _bind(component: str) -> Callable[[str, str, Optional[str]], None]:
            def _emit(phase: str, detail: str = "", reason: Optional[str] = None) -> None:
                self._emit_init(component, phase, detail, reason)
            return _emit

        for name in _INIT_MOTOR_COMPONENTS:
            motor = self._get_motor_by_name(name)
            if motor is not None:
                motor._on_init_progress = _bind(name)

        gripper = getattr(self._tool, "_gs", None)
        if gripper is not None:
            gripper._on_init_progress = _bind("gripper")

    def get_init_component_names(self) -> List[str]:
        names: List[str] = ["motor_bus"]

        io_scale_config = self._config.get("robot.io.scale", None)
        scale_enabled = getattr(io_scale_config, "enabled", True) if io_scale_config is not None else True
        if scale_enabled is True and self._scalebus is not self._motorbus:
            names.append("scale_bus")

        laser_config = self._config.get("robot.tool.laser", None)
        laser_enabled = getattr(laser_config, "enabled", False) if laser_config is not None else False
        if (
            laser_enabled is True
            and self._laserbus is not self._motorbus
            and self._laserbus is not self._scalebus
        ):
            names.append("laser_bus")

        info = self.get_motor_info()
        if self._tool._params.gripper.enabled is True:
            names.append("gripper")
        for motor in info.get("tool", []):
            names.append(str(motor["name"]))
        if self._tool._laser is not None:
            names.append("laser")
        for motor in info.get("io", []):
            names.append(str(motor["name"]))
        if getattr(self._io, "_scale_enabled", False) is True:
            names.append("scale")
        if self._is_curtain_enabled() is True:
            names.append("light_curtain")
        if getattr(self, "_cabinet", None) is not None:
            names.append("cabinet")

        return names

    def _is_curtain_enabled(self) -> bool:
        io_params = getattr(self._io, "_params", None)
        curtain_cfg = getattr(io_params, "curtain", None)
        if curtain_cfg is None:
            return False
        return bool(getattr(curtain_cfg, "enabled", False))

    async def configure(self, isolate: bool = False, home_on_init: bool = True):
        self._emit_init("motor_bus", "connecting")
        try:
            await self._connect_controller("motor", self._motorbus, self._bus_settings["motor"])
        except Exception as exc:
            self._emit_init("motor_bus", "failed", reason=str(exc))
            for name in _INIT_MOTOR_COMPONENTS:
                if self._get_motor_by_name(name) is not None:
                    self._emit_init(name, "failed", reason="motor bus down")
            raise
        self._emit_init("motor_bus", "ready")

        io_scale_config = self._config.get("robot.io.scale", None)
        scale_enabled = getattr(io_scale_config, "enabled", True) if io_scale_config is not None else True
        laser_config = self._config.get("robot.tool.laser", None)
        laser_enabled = getattr(laser_config, "enabled", False) if laser_config is not None else False

        scale_bus_ready = True
        if scale_enabled is True:
            if self._scalebus is self._motorbus:
                self._logger.info("scale_bus_shared_with_motor")
            else:
                scale_bus_ready = await self._connect_optional_bus("scale", self._scalebus, isolate)
        else:
            self._logger.info("scale_bus_skipped: scale is disabled in config")

        if laser_enabled is True:
            if self._config.robot.bus is None or self._config.robot.bus.laser is None:
                raise ConfigError("Laser bus configuration is required when laser is enabled")
            if self._laserbus is self._motorbus:
                self._logger.info("laser_bus_shared_with_motor")
            elif self._laserbus is self._scalebus:
                self._logger.info("laser_bus_shared_with_scale")
            else:
                await self._connect_optional_bus("laser", self._laserbus, isolate)
        else:
            self._logger.info("laser_bus_skipped: laser is disabled in config")

        gpio = getattr(self, "_gpio", None)
        if gpio is not None:
            gpio.apply_initial_outputs()
            gpio.observe_all_inputs()

        # The pre-home clearance jog happens during tool.configure, before the
        # worker exists, so the guard has to start sampling here or the boot-time
        # crush scenario goes unguarded. The worker replaces the listener when it
        # connects; start() is idempotent so its later start is a no-op.
        weight_guard = getattr(self, "_weight_guard", None)
        if weight_guard is not None:
            weight_guard.set_listener(self._stop_motion_on_weight_change)
            await weight_guard.start()

        await self._tool.configure(isolate=isolate, home_on_init=home_on_init)
        await self._io.configure(isolate=isolate, scale_bus_ready=scale_bus_ready, home_on_init=home_on_init)
        cabinet = getattr(self, "_cabinet", None)
        if cabinet is not None:
            self._emit_init("cabinet", "configuring")
            try:
                await cabinet.configure()
                self._emit_init("cabinet", "ready")
            except Exception as exc:
                self._emit_init("cabinet", "failed", reason=str(exc))
                if isolate is False:
                    raise

        for name in _INIT_MOTOR_COMPONENTS:
            motor = self._get_motor_by_name(name)
            if motor is not None:
                motor._on_move_complete = self._did_complete_motor_move

    async def _connect_optional_bus(self, label: str, controller: Controller, isolate: bool) -> bool:
        self._emit_init(f"{label}_bus", "connecting")
        try:
            await self._connect_controller(label, controller, self._bus_settings[label])
            self._emit_init(f"{label}_bus", "ready")
            return True
        except Exception as exc:
            self._emit_init(f"{label}_bus", "failed", reason=str(exc))
            if isolate is False:
                raise
            return False

    async def shutdown(self) -> None:
        await self.stop()

        for motor in [self._tool._mx, self._tool._my, self._tool._mg, self._tool._ms, self._tool._mr, self._io._md]:
            if motor is not None:
                motor.cleanup()

        gpio = getattr(self, "_gpio", None)
        if gpio is not None:
            gpio.close()

        if self._motorbus:
            await self._motorbus.close()
        if self._scalebus:
            await self._scalebus.close()
        if self._laserbus:
            await self._laserbus.close()

    async def stop(self) -> None:
        await self._tool.stop()
        await self._io.stop()
        cabinet = getattr(self, "_cabinet", None)
        if cabinet is not None:
            await cabinet.stop()

    def get_power_rail_states(self) -> Dict[str, Optional[bool]]:
        """Whether each rail is currently powered. None if the pin is unavailable.

        The pin is asserted to CUT power, so powered is its inverse.
        """
        gpio = getattr(self, "_gpio", None)
        states: Dict[str, Optional[bool]] = {}

        for rail in POWER_RAILS:
            if gpio is None or gpio.has_pin(rail) is False:
                states[rail] = None
                continue
            try:
                states[rail] = not gpio.read_output(rail)
            except Exception:
                states[rail] = None

        return states

    async def _wait_for_drive(self, motor: "Motor") -> bool:
        """Poll a drive until it answers after a power cycle."""
        deadline = asyncio.get_running_loop().time() + POWER_CYCLE_BOOT_TIMEOUT_S

        while asyncio.get_running_loop().time() < deadline:
            try:
                await motor.get_param("Motion status")
                return True
            except Exception:
                await asyncio.sleep(POWER_CYCLE_BOOT_POLL_S)

        return False

    async def _reconfigure_after_power_cycle(self, axes: tuple[str, ...]) -> None:
        """Re-apply runtime parameters the drives lost with their power.

        move_config() -- all that homing runs -- restores the path mode,
        direction and velocity. Peak current, accel/decel, the position loop
        gain, the stop time and the following-error limit come only from
        configure(), so without this a home after a power cycle runs on EEPROM
        values and faults.
        """
        failed_axes: list[str] = []

        for name in axes:
            motor = self._get_motor_by_name(name)
            if motor is None:
                continue

            if await self._wait_for_drive(motor) is False:
                self._logger.error(
                    "drive did not answer after power cycle; parameters not restored",
                    extra={"motor_name": name, "timeout_s": POWER_CYCLE_BOOT_TIMEOUT_S},
                )
                failed_axes.append(name)
                continue

            try:
                await motor.configure()
            except Exception as e:
                self._logger.error(
                    "failed to reconfigure after power cycle",
                    extra={"motor_name": name, "error": str(e)},
                )
                failed_axes.append(name)

        if failed_axes:
            raise MotorError(f"Power cycle reconfigure failed for axes: {', '.join(failed_axes)}")

    async def power_cycle_rail(
        self,
        rail: str,
        *,
        on_cut: Callable[[], None] | None = None,
    ) -> tuple[str, ...]:
        """Cut and restore one switched power rail. Returns the axes it dropped.

        Drive settings that only latch at power-on need this: any DI/DO function
        change is written, verified and saved, yet stays inert until the drive
        restarts (CL57RS manual 4.3.2). Anything on the rail comes back with its
        RAM parameters lost and, for a motor, an incremental encoder that no
        longer knows where it is -- so those axes are marked unhomed and their
        drives reconfigured once they answer again. The caller still has to home
        them.

        The optional ``on_cut`` callback runs synchronously after power is cut and
        before the hold begins.
        """
        if rail not in POWER_RAILS:
            raise ConfigError(f"Unknown power rail '{rail}'")

        spec = POWER_RAILS[rail]

        for name in spec.axes:
            motor = self._get_motor_by_name(name)
            if motor is not None and motor._status is MotorStatus.MOVING:
                raise MotorError(f"Refusing to power cycle {rail} while {name} is moving")

        gpio = getattr(self, "_gpio", None)
        if gpio is None or gpio.has_pin(rail) is False:
            raise ConfigError(f"GPIO pin {rail} is not configured")

        for name in spec.axes:
            motor = self._get_motor_by_name(name)
            if motor is None:
                continue
            # Still booting; nothing may move on stale coordinates.
            motor._is_homed = False
            motor._hard_stopped = True
            motor._cur_pos = None

        if spec.gripper is True:
            gripper = getattr(getattr(self, "_tool", None), "_gs", None)
            if gripper is not None:
                # _ensure_configured() reconfigures it on next use.
                gripper._configured = False

        output = gpio.output(rail)
        self._logger.warning(
            "Power cycling rail",
            extra={"pin": rail, "supplies": spec.supplies, "hold_s": POWER_CYCLE_HOLD_S},
        )

        output.write(True)  # asserted cuts power
        if on_cut is not None:
            on_cut()
        try:
            await asyncio.sleep(POWER_CYCLE_HOLD_S)
        finally:
            output.write(False)  # cleared restores it
            configure_error: MotorError | None = None
            try:
                await self._reconfigure_after_power_cycle(spec.axes)
            except MotorError as exc:
                # _reconfigure_after_power_cycle already logged per-axis failure reasons.
                configure_error = exc

        if configure_error is not None:
            raise configure_error

        return spec.axes

    async def idle(self) -> None:
        idle_position = await self._tool.get_param("IDLE_POSITION")
        if idle_position is None:
            raise ConfigError("IDLE_POSITION not configured")

        try:
            ix, iy = idle_position
        except (TypeError, ValueError) as exc:
            raise ConfigError("IDLE_POSITION must be a 2-element iterable [x, y]") from exc

        await self._tool.move(AxisCoordinate(ix, iy))

    # ------------------------------------------------------------------ #
    #  Bus management
    # ------------------------------------------------------------------ #

    def _prepare_bus_settings(self) -> Dict[str, Dict[str, Any]]:
        defaults: Dict[str, Dict[str, Any]] = {
            "motor": {
                "port": DEFAULT_MOTOR_PORT,
                "fallback_ports": MOTOR_PORT_CANDIDATES,
                "baudrate": 115200,
                "parity": "N",
                "timeout": 2.0,
                "retry_delay_s": 0.25,
            },
            "scale": {
                "port": DEFAULT_SCALE_PORT,
                "fallback_ports": SCALE_PORT_CANDIDATES,
                "baudrate": 9600,
                "parity": "N",
                "timeout": 2.0,
                "retry_delay_s": 0.25,
            },
            "laser": {
                "port": DEFAULT_LASER_PORT,
                "fallback_ports": LASER_PORT_CANDIDATES,
                "baudrate": 115200,
                "parity": "N",
                "timeout": 2.0,
                "retry_delay_s": 0.25,
            },
        }
        bus_cfg = getattr(self._config.robot, "bus", None)

        def build(name: str) -> Dict[str, Any]:
            cfg = getattr(bus_cfg, name, None) if bus_cfg else None
            settings = defaults[name].copy()
            if cfg is not None:
                port = cfg.port
                default_fallback_ports = list(defaults[name]["fallback_ports"])
                fallback_candidates = [port, *default_fallback_ports] if port in default_fallback_ports else [port]
                fallback_ports = list(dict.fromkeys(fallback_candidates))
                settings.update(
                    {
                        "port": port,
                        "fallback_ports": fallback_ports,
                        "baudrate": cfg.baudrate,
                        "parity": cfg.parity,
                        "timeout": cfg.timeout,
                        "retry_delay_s": cfg.retry_delay_s,
                    }
                )
            return settings

        return {
            "motor": build("motor"),
            "scale": build("scale"),
            "laser": build("laser"),
        }

    def _create_controller_for_bus(self, name: str) -> Controller:
        if name == "scale" and self._bus_settings_match("scale", "motor") is True:
            return self._motorbus

        if name == "laser" and self._bus_settings_match("laser", "motor") is True:
            return self._motorbus

        if name == "laser" and self._bus_settings_match("laser", "scale") is True:
            return self._scalebus

        settings = self._bus_settings[name]

        return Controller(
            port=settings["port"],
            baudrate=settings["baudrate"],
            parity=settings["parity"],
            timeout=settings["timeout"],
        )

    def _bus_settings_match(self, left: str, right: str) -> bool:
        left_settings = self._bus_settings[left]
        right_settings = self._bus_settings[right]

        return (
            left_settings["port"] == right_settings["port"]
            and left_settings["baudrate"] == right_settings["baudrate"]
            and left_settings["parity"] == right_settings["parity"]
            and left_settings["timeout"] == right_settings["timeout"]
        )

    @staticmethod
    def _connection_ports(settings: Dict[str, Any]) -> List[str]:
        ports: List[str] = []
        for port in [settings["port"], *settings.get("fallback_ports", [])]:
            if port not in ports:
                ports.append(port)
        return ports

    async def _connect_controller(self, label: str, controller: Controller, settings: Dict[str, Any]) -> None:
        errors: List[str] = []
        for port in Robot._connection_ports(settings):
            controller.port = port
            controller.baudrate = settings["baudrate"]
            controller.parity = settings["parity"]
            controller.timeout = settings["timeout"]

            try:
                await controller.connect()
            except Exception as exc:
                errors.append(f"{port}: {exc}")
                await controller.close()
            else:
                if controller.client and controller.client.connected:
                    return

                errors.append(f"{port}: client not connected")
                await controller.close()

            delay = settings.get("retry_delay_s", 0)
            if delay:
                await asyncio.sleep(delay)

        detail = "; ".join(errors) if errors else "no ports configured"
        raise ConfigError(f"{label.capitalize()} bus connection failed ({detail})")

    # ------------------------------------------------------------------ #
    #  Motor time tracking
    # ------------------------------------------------------------------ #

    def _did_complete_motor_move(self, elapsed: float) -> None:
        self._motor_time_accumulator += elapsed

    def drain_motor_time(self) -> float:
        value = self._motor_time_accumulator
        self._motor_time_accumulator = 0.0
        return value

    # ------------------------------------------------------------------ #
    #  Diagnostics and operation context
    # ------------------------------------------------------------------ #

    @staticmethod
    def _safe_model_dump(value: Any) -> Any:
        if value is None:
            return None
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json")
        if isinstance(value, dict):
            return {key: Robot._safe_model_dump(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [Robot._safe_model_dump(item) for item in value]
        if hasattr(value, "__dataclass_fields__"):
            return {key: Robot._safe_model_dump(getattr(value, key)) for key in value.__dataclass_fields__}
        return value

    def set_operation_context(
        self,
        *,
        command: str | None = None,
        phase: str | None = None,
        target_cell: Any = None,
        target_coordinate: Any = None,
        item: Any = None,
        nudge_override: float | None = None,
    ) -> None:
        updates: dict[str, Any] = {}
        if command is not None:
            updates["command"] = command
        if phase is not None:
            updates["phase"] = phase
        if target_cell is not None:
            updates["target_cell"] = self._safe_model_dump(target_cell)
        if target_coordinate is not None:
            updates["target_coordinate"] = self._safe_model_dump(target_coordinate)
        if item is not None:
            updates["item"] = self._safe_model_dump(item)
        if nudge_override is not None:
            updates["nudge_override"] = nudge_override

        self._operation_context = {**self._operation_context, **updates}
        listener = getattr(self, "_operation_context_listener", None)
        if listener is not None:
            listener(self.get_operation_context())

    def clear_operation_context(self) -> None:
        self._operation_context = {}
        listener = getattr(self, "_operation_context_listener", None)
        if listener is not None:
            listener(None)

    def set_operation_context_listener(
        self,
        listener: Callable[[dict[str, Any] | None], None] | None,
    ) -> None:
        self._operation_context_listener = listener

    def get_operation_context(self) -> dict[str, Any] | None:
        if not self._operation_context:
            return None
        return self._safe_model_dump(self._operation_context)

    def _capture_fault_context(self, err: BaseException) -> None:
        fault_context = getattr(err, "fault_context", None)
        self._last_fault_context = {
            "operation_context": self.get_operation_context(),
            "exception_type": type(err).__name__,
            "message": str(err),
        }
        if isinstance(fault_context, dict):
            self._last_fault_context["tool_fault"] = self._safe_model_dump(fault_context)

    def get_last_fault_context(self) -> dict[str, Any] | None:
        if self._last_fault_context is None:
            return None
        return self._safe_model_dump(self._last_fault_context)

    def _motor_diagnostic_snapshot(self, axis: str, motor: Optional["Motor"]) -> dict[str, Any]:
        snapshot: dict[str, Any] = {"axis": axis, "configured": motor is not None}
        if motor is None:
            return snapshot

        try:
            snapshot["motor_id"] = getattr(motor, "_motor_id", None)
        except Exception as exc:
            snapshot["motor_id_error"] = str(exc)

        for key, attr in (
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

        for key, reader in (
            ("homed", motor.is_homed),
            ("hard_stopped", motor.is_hard_stopped),
        ):
            try:
                snapshot[key] = reader()
            except Exception as exc:
                snapshot[f"{key}_error"] = str(exc)

        try:
            params = getattr(motor, "_params", None)
            snapshot["lower_limit_mm"] = getattr(params, "lower_lim", None)
            snapshot["limit_mm"] = getattr(params, "lim", None)
        except Exception as exc:
            snapshot["limits_error"] = str(exc)

        return snapshot

    async def get_diagnostic_snapshot(self) -> dict[str, Any]:
        motors: dict[str, dict[str, Any]] = {}
        for axis in ["x_axis", "y_axis", "z_axis", "stage", "rotate", "door"]:
            try:
                motors[axis] = self._motor_diagnostic_snapshot(axis, self._get_motor_by_name(axis))
            except Exception as exc:
                motors[axis] = {"axis": axis, "snapshot_error": str(exc)}

        snapshot: dict[str, Any] = {
            "current_cell": self._safe_model_dump(self._state.current_cell),
            "operation_context": self.get_operation_context(),
            "last_fault_context": self.get_last_fault_context(),
            "motors": motors,
            "io": {},
        }

        try:
            snapshot["io"]["curtain_blocked"] = self.get_curtain_state()
        except Exception as exc:
            snapshot["io"]["curtain_blocked_error"] = str(exc)

        try:
            snapshot["io"]["door_open"] = self.get_door_state()
        except Exception as exc:
            snapshot["io"]["door_open_error"] = str(exc)

        try:
            snapshot["cabinet"] = {"doors": self.get_cabinet_door_state()}
        except Exception as exc:
            snapshot["cabinet"] = {"doors_error": str(exc)}

        return snapshot

    # ------------------------------------------------------------------ #
    #  Demo cell mapping
    # ------------------------------------------------------------------ #

    # One-time on-disk migration of the demo mapping files written before the front/back
    # cutover, which keyed their cell locations under the L/R side letters. The shared
    # implementation lives in openshelf_os.demo_side_rename so other demo.json readers
    # (e.g. scripts/seed_demo.py) migrate on load with the same semantics.
    # Kept as a class attribute alias of the shared constant for existing references.
    DEMO_SIDE_RENAME_BACKUP_SUFFIX = DEMO_SIDE_RENAME_BACKUP_SUFFIX

    @classmethod
    def _migrate_demo_side_rename(cls, target_path: str, raw_content: str, data: Any) -> Any:
        """Rename the pre-cutover side letters in a demo mapping file, in place and idempotently.

        Files written before the front/back cutover keep their cell locations under the
        L/R side letters. Rename them in the loaded data and rewrite the file with an
        exclusive-created backup of the original bytes, so a device picks the new letters
        up on its next load and the old file is recoverable. Files that already carry
        the new letters are left untouched.
        """
        return migrate_demo_side_rename(target_path, raw_content, data)

    @staticmethod
    def _parse_demo_location(value: str) -> StorageModuleCellLocation:
        side_map = {
            "F": StorageModuleSide.FRONT,
            "B": StorageModuleSide.BACK,
            "FRONT_MODULE": StorageModuleSide.FRONT,
            "BACK_MODULE": StorageModuleSide.BACK,
        }
        parts = value.split(":")
        if len(parts) != 3:
            raise ValueError(f"invalid demo location format: {value!r}")

        side_key = parts[0].strip().upper()
        if side_key not in side_map:
            raise ValueError(f"invalid demo location format: {value!r}")

        return StorageModuleCellLocation(
            side=side_map[side_key],
            shelf_idx=int(parts[1]),
            cell_idx=int(parts[2]),
        )

    def _load_demo_cell_map(self, path: str) -> dict[str, StorageModuleCellLocation] | None:
        file_path = _Path(path)
        if file_path.exists() is False:
            self._logger.warning("demo_cell_map_missing: %s", path)
            return None

        raw_content = file_path.read_text(encoding="utf-8")
        raw = _json.loads(raw_content)
        raw = self._migrate_demo_side_rename(str(file_path), raw_content, raw)
        if isinstance(raw, dict) is False:
            raise ValueError("demo mapping must be a JSON object")

        self._demo_nudge_map = {}
        cell_map: dict[str, StorageModuleCellLocation] = {}

        legacy_mapping = raw.get("code_cell_mapping")
        if isinstance(legacy_mapping, dict):
            for barcode, loc_str in legacy_mapping.items():
                if isinstance(loc_str, str) is False:
                    raise ValueError(f"invalid legacy demo cell mapping for {barcode!r}")
                cell_map[str(barcode)] = self._parse_demo_location(loc_str)

            nudge_raw = raw.get("nudge_override_mm", {})
            if isinstance(nudge_raw, dict) is False:
                raise ValueError("legacy nudge_override_mm must be an object")
            for loc_str, nudge_mm in nudge_raw.items():
                if isinstance(loc_str, str) is False:
                    raise ValueError("legacy nudge_override_mm keys must be location strings")
                loc = self._parse_demo_location(loc_str)
                self._demo_nudge_map[(loc.side.value, loc.shelf_idx, loc.cell_idx)] = float(nudge_mm)
        else:
            for barcode, payload in raw.items():
                if isinstance(payload, str):
                    cell_map[str(barcode)] = self._parse_demo_location(payload)
                    continue

                if isinstance(payload, dict) is False:
                    raise ValueError(f"invalid demo entry payload for {barcode!r}")

                cell_value = payload.get("cell")
                if isinstance(cell_value, str) is False:
                    raise ValueError(f"demo entry for {barcode!r} must include string field 'cell'")

                location = self._parse_demo_location(cell_value)
                cell_map[str(barcode)] = location

                nudge_mm = payload.get("nudge_override_mm")
                if nudge_mm is not None:
                    self._demo_nudge_map[(location.side.value, location.shelf_idx, location.cell_idx)] = float(nudge_mm)

        self._logger.info(
            "demo_cell_map_loaded: %d entries, %d nudge overrides from %s",
            len(cell_map),
            len(self._demo_nudge_map),
            path,
        )
        return cell_map

    def _get_demo_nudge(self, location: StorageModuleCellLocation) -> float | None:
        return self._demo_nudge_map.get(
            (location.side.value, location.shelf_idx, location.cell_idx)
        )

    # ------------------------------------------------------------------ #
    #  Grid state
    # ------------------------------------------------------------------ #

    def sync_occupied_cells(self, occupied: list[tuple[str, int, int]]) -> None:
        if self._grid is None:
            return

        self._grid.clear_all_items()

        occupied_set = set(occupied)
        for side, shelf_idx, cell_idx in occupied_set:
            try:
                side_enum = StorageModuleSide(side)
                location = StorageModuleCellLocation(side=side_enum, shelf_idx=shelf_idx, cell_idx=cell_idx)
                self._grid.store_item(location, None)
            except (ValueError, IndexError) as e:
                self._logger.warning(
                    "sync_occupied_cells_skip: side=%s shelf=%d cell=%d error=%s",
                    side, shelf_idx, cell_idx, str(e)
                )

    def set_cell_status(self, side: str, shelf_idx: int, cell_idx: int, status: CellStatus) -> None:
        """Apply a server-owned reservation without presenting it as physical inventory."""
        if self._grid is None:
            return
        location = StorageModuleCellLocation(
            side=StorageModuleSide(side), shelf_idx=shelf_idx, cell_idx=cell_idx
        )
        cell = self._grid.get_cell(location)
        cell.status = status
        if status != CellStatus.OCCUPIED:
            cell.item = None

    def set_reserved_cell_locations(self, locations: Iterable[tuple[str, int, int]]) -> None:
        """Set server-owned homes that automatic storage allocation must avoid."""
        configured_locations: set[tuple[str, int, int]] = {
            (entry.home_location.side, entry.home_location.shelf_idx, entry.home_location.cell_idx)
            for entry in self._config.storage.bins
        }
        self._reserved_cell_locations = configured_locations | set(locations)


    # ------------------------------------------------------------------ #
    #  Status
    # ------------------------------------------------------------------ #

    async def _build_axis_status(self, motor: Optional["Motor"]) -> AxisTelemetry:
        position = 0.0
        if motor is None:
            return AxisTelemetry(position=position)
        try:
            position = await motor.get_position()
        except Exception as exc:
            self._logger.debug("Failed to read motor position: %s", exc)
        return AxisTelemetry(
            position=position,
            homed=int(motor.is_homed()),
            hard_stopped=int(motor.is_hard_stopped()),
        )

    async def get_status(self) -> RobotStatus:
        gantry_status = GantryStatus(
            x_axis=await self._build_axis_status(getattr(self._tool, "_mx", None)),
            y_axis=await self._build_axis_status(getattr(self._tool, "_my", None)),
        )
        tool_status = ToolAxisStatus(
            z_axis=await self._build_axis_status(getattr(self._tool, "_mg", None)),
            stage=await self._build_axis_status(getattr(self._tool, "_ms", None)),
            rotate=await self._build_axis_status(getattr(self._tool, "_mr", None)),
        )
        state = RobotState(current_cell=self._state.current_cell)
        cabinet_doors_data = self.get_cabinet_door_state()
        cabinet_doors = (
            CabinetDoorState(**cabinet_doors_data)
            if cabinet_doors_data is not None
            else None
        )
        status = RobotStatus(
            gantry=gantry_status,
            tool=tool_status,
            state=state,
            storage=self._grid,
            cabinet_doors=cabinet_doors,
            io_door_open=self.get_door_state(),
            motors={
                "x_axis": gantry_status.x_axis,
                "y_axis": gantry_status.y_axis,
                "z_axis": tool_status.z_axis,
                "stage": tool_status.stage or AxisTelemetry(),
                "rotate": tool_status.rotate or AxisTelemetry(),
                "door": await self._build_axis_status(getattr(self._io, "_md", None)),
            },
        )
        return status

    # ------------------------------------------------------------------ #
    #  Safety / door
    # ------------------------------------------------------------------ #

    def register_light_curtain_listener(self, listener) -> None:
        if self._io:
            self._io.set_curtain_listener(listener)

    def register_cabinet_door_listener(self, listener) -> None:
        cabinet = getattr(self, "_cabinet", None)
        if cabinet:
            cabinet.set_door_listener(listener)

    def set_safety_guard_bypassed(self, enabled: bool) -> None:
        if self._io:
            self._io.set_safety_guard_bypassed(enabled)

    def set_light_curtain_bypassed(self, enabled: bool) -> None:
        if self._io:
            self._io.set_light_curtain_bypassed(enabled)

    def block_motion_for_safety(self) -> None:
        self._motion_safety.block()
        self._logger.info("Safety guard engaged")

    def release_motion_safety(self) -> None:
        self._motion_safety.unblock()
        self._logger.info("Safety guard cleared")

    def _build_weight_guard(self) -> Optional[WeightChangeGuard]:
        """Watch the scale during downward y travel so a crash into the cell stops motion.

        Returns None when the scale or the y axis is absent, or when the guard is
        switched off in config, in which case the safety sources simply omit it.
        """
        scale_cfg = self._config.get("robot.io.scale", None)

        if scale_cfg is None or getattr(scale_cfg, "enabled", True) is not True:
            return None

        if getattr(scale_cfg, "motion_guard_enabled", True) is not True:
            self._logger.info("weight_guard_disabled")

            return None

        scale = getattr(self._io, "_scale", None)
        y_motor = getattr(self._tool, "_my", None)

        if scale is None or y_motor is None:
            self._logger.info("weight_guard_unavailable: scale or y axis not configured")

            return None

        return WeightChangeGuard(
            scale=scale,
            axis=y_motor,
            delta_grams=float(getattr(scale_cfg, "motion_guard_delta_grams", 10.0)),
            min_samples=int(getattr(scale_cfg, "motion_guard_min_samples", 2)),
            arm_delay_s=float(getattr(scale_cfg, "motion_guard_arm_delay_s", 0.05)),
            ack_grace_s=float(getattr(scale_cfg, "motion_guard_ack_grace_s", 1.0)),
            sample_timeout_s=float(getattr(scale_cfg, "motion_guard_sample_timeout_s", 0.25)),
            idle_interval_s=float(getattr(scale_cfg, "motion_guard_idle_interval_s", 0.2)),
            context_provider=lambda: dict(self.get_operation_context() or {}),
        )

    def register_weight_change_listener(self, listener) -> None:
        if self._weight_guard is not None:
            self._weight_guard.set_listener(listener)

    def _stop_motion_on_weight_change(self, info: dict[str, Any]) -> None:
        """Trip listener for the window before the worker takes over.

        Only the drive has to stop here; the worker latches the safety state and
        publishes the event once it connects and replaces this listener.
        """
        self.block_motion_for_safety()
        self._logger.warning(
            "Weight change guard tripped during configuration",
            extra=info,
        )

    async def start_weight_guard(self) -> None:
        if self._weight_guard is not None:
            await self._weight_guard.start()

    async def stop_weight_guard(self) -> None:
        if self._weight_guard is not None:
            await self._weight_guard.stop()

    def acknowledge_weight_change(self) -> None:
        if self._weight_guard is not None:
            self._weight_guard.acknowledge()

    def get_weight_change_state(self) -> Optional[dict[str, Any]]:
        if self._weight_guard is None:
            return None

        return self._weight_guard.source_state()

    def get_curtain_state(self) -> Optional[bool]:
        if self._io:
            return self._io.get_curtain_state()
        return None

    def get_door_state(self) -> Optional[bool]:
        if self._io:
            return self._io.get_door_state()
        return None

    def get_cabinet_door_state(self) -> dict[str, bool] | None:
        cabinet = getattr(self, "_cabinet", None)
        if cabinet is None:
            return None

        try:
            return cabinet.get_door_state()
        except CabinetConfigError:
            return None

    async def refresh_door_state(self) -> Optional[bool]:
        if self._io:
            return await self._io.refresh_door_state()
        return None

    # ------------------------------------------------------------------ #
    #  Pick and place
    # ------------------------------------------------------------------ #

    async def io_place(self, coord: AxisCoordinate, nudge_override: float | None = None):
        ppl = await self._tool.get_param("PICKUP_POSITION_L")
        pph = await self._tool.get_param("PICKUP_POSITION_H")
        if ppl is None or pph is None:
            raise ConfigError("Missing PICKUP_POSITION_L or PICKUP_POSITION_H parameter")

        if self._tool._my is None:
            raise ConfigError("Y-axis motor not configured")

        self.set_operation_context(phase="io_place.ensure_door_homed")
        await self._io.ensure_door_homed()
        self.set_operation_context(phase="io_place.pick_from_shelf", target_coordinate=coord)
        await self._tool.pick(coord)
        self.set_operation_context(phase="io_place.rotate_to_front")
        await self._tool.rotate_to_position("front")
        self.set_operation_context(phase="io_place.move_to_io_position")
        await self._tool.move(AxisCoordinate(ppl[0], ppl[1]))
        self.set_operation_context(phase="io_place.open_door")
        await self._io.open_door()
        self.set_operation_context(phase="io_place.extend_into_io", nudge_override=nudge_override)
        await self._tool.extend_IO(nudge_override=nudge_override)
        self.set_operation_context(phase="io_place.y_axis_transfer")
        await self._tool._my.move_to_position(pph[1])
        self.set_operation_context(phase="io_place.retract")
        await self._tool.retract()
        self.set_operation_context(phase="io_place.close_door")
        await self._io.close_door()
        self.set_operation_context(phase="io_place.return_to_shelf_coordinate")
        await self._tool.move(coord)
        self.set_operation_context(phase="io_place.place_at_shelf_coordinate")
        await self._tool.place(coord)

        self.set_operation_context(phase="io_place.home_door")
        await self._io.home_door_after_cycle()
        self.set_operation_context(phase="io_place.clear_alarms")
        await self._tool.clear_alarms()

    async def io_pick(self, coord: AxisCoordinate):
        ppl = await self._tool.get_param("PICKUP_POSITION_L")
        pph = await self._tool.get_param("PICKUP_POSITION_H")
        if ppl is None or pph is None:
            raise ConfigError("Missing PICKUP_POSITION_L or PICKUP_POSITION_H parameter")

        if self._tool._my is None:
            raise ConfigError("Y-axis motor not configured")

        self.set_operation_context(phase="io_pick.ensure_door_homed")
        await self._io.ensure_door_homed()
        self.set_operation_context(phase="io_pick.pick_from_shelf_cell", target_coordinate=coord)
        await self._tool.pick(coord)
        self.set_operation_context(phase="io_pick.rotate_to_front")
        await self._tool.rotate_to_position("front")
        self.set_operation_context(phase="io_pick.move_to_io_high_position")
        await self._tool.move(AxisCoordinate(pph[0], pph[1]))
        self.set_operation_context(phase="io_pick.open_door")
        await self._io.open_door()
        self.set_operation_context(phase="io_pick.extend_into_io")
        await self._tool.extend()
        self.set_operation_context(phase="io_pick.y_axis_transfer")
        await self._tool._my.move_to_position(ppl[1])
        self.set_operation_context(phase="io_pick.retract")
        await self._tool.retract()
        self.set_operation_context(phase="io_pick.close_door")
        await self._io.close_door()
        self.set_operation_context(phase="io_pick.return_to_shelf_coordinate")
        await self._tool.move(coord)
        self.set_operation_context(phase="io_pick.place_back_at_shelf_coordinate")
        await self._tool.place(coord)

        self.set_operation_context(phase="io_pick.home_door")
        await self._io.home_door_after_cycle()
        self.set_operation_context(phase="io_pick.clear_alarms")
        await self._tool.clear_alarms()

    async def _set_tray_grip(self, *, engaged: bool) -> None:
        """Route tray servo failures through the command hardware-fault interlock."""
        try:
            if engaged:
                require_grip(
                    await self._tool._gs.open(),
                    "tray grip",
                    is_enabled=self._tool.is_grip_detection_enabled,
                )
            else:
                await self.release_gripper()
        except Exception as exc:
            action = "grip" if engaged else "release"
            raise GripperError(f"Tray {action} failed: {exc}") from exc

    async def deliver_tray(self, coord: AxisCoordinate) -> None:
        """Leave the entire tray supported in the IO box, with the tool clear."""
        position = await self._tool.get_param("PICKUP_POSITION_L")
        if position is None:
            raise ConfigError("Missing PICKUP_POSITION_L parameter")
        self.set_operation_context(phase="deliver_tray.pick_from_shelf", target_coordinate=coord)
        await self._tool.pick(coord)
        self.set_operation_context(phase="deliver_tray.rotate_to_front")
        await self._tool.rotate_to_position("front")
        self.set_operation_context(phase="deliver_tray.move_to_io")
        await self._tool.move(AxisCoordinate(position[0], position[1]))
        self.set_operation_context(phase="deliver_tray.open_door")
        await self._io.open_door()
        self.set_operation_context(phase="deliver_tray.extend_into_io")
        # Use the same supported pose for delivery and collection. No item-transfer
        # Y motion: the tray itself stays here instead of lifting off its contents.
        await self._tool.extend_IO(nudge_override=0.0)
        self.set_operation_context(phase="deliver_tray.release")
        await self._set_tray_grip(engaged=False)
        self.set_operation_context(phase="deliver_tray.retract_empty")
        await self._tool.retract_empty()
        self.set_operation_context(phase="deliver_tray.close_door")
        await self._io.close_door()
        self.set_operation_context(phase="deliver_tray.home_door")
        await self._io.home_door()
        await self._tool.clear_alarms()

    async def return_tray(self, coord: AxisCoordinate) -> None:
        """Collect the released tray at the delivery pose, then replace it at home."""
        position = await self._tool.get_param("PICKUP_POSITION_L")
        if position is None:
            raise ConfigError("Missing PICKUP_POSITION_L parameter")
        self.set_operation_context(phase="return_tray.release")
        await self._set_tray_grip(engaged=False)
        self.set_operation_context(phase="return_tray.rotate_to_front")
        await self._tool.rotate_to_position("front")
        self.set_operation_context(phase="return_tray.move_to_io")
        await self._tool.move(AxisCoordinate(position[0], position[1]))
        self.set_operation_context(phase="return_tray.open_door")
        await self._io.open_door()
        self.set_operation_context(phase="return_tray.extend_into_io")
        await self._tool.extend_IO(nudge_override=0.0)
        self.set_operation_context(phase="return_tray.grip")
        await self._set_tray_grip(engaged=True)
        self.set_operation_context(phase="return_tray.retract")
        await self._tool.retract()
        try:
            require_grip(
                await self._tool._gs.read_state(),
                "tray retract",
                is_enabled=self._tool.is_grip_detection_enabled,
            )
        except Exception as exc:
            raise GripperError(f"Tray retract grip verification failed: {exc}") from exc
        self.set_operation_context(phase="return_tray.close_door")
        await self._io.close_door()
        self.set_operation_context(phase="return_tray.place_at_home", target_coordinate=coord)
        await self._tool.place(coord)
        self.set_operation_context(phase="return_tray.home_door")
        await self._io.home_door()
        await self._tool.clear_alarms()

    # ------------------------------------------------------------------ #
    #  Store / retrieve
    # ------------------------------------------------------------------ #

    async def store(self, item_payload: Any, *, tray: bool = False) -> RobotResponse:
        command_name = "store_item"

        if not self._grid:
            return RobotCommandError(command=command_name, error="Storage grid not calibrated")

        resolved_barcode: str
        resolved_item_name: str
        if isinstance(item_payload, str):
            resolved_barcode = item_payload
            resolved_item_name = item_payload
        elif isinstance(item_payload, dict):
            resolved_barcode = item_payload.get("barcode") or item_payload.get("name") or "item"
            resolved_item_name = item_payload.get("name") or resolved_barcode
        else:
            resolved_barcode = "item"
            resolved_item_name = "item"

        provided_dimensions = None
        if isinstance(item_payload, dict):
            provided_dimensions = item_payload.get("dimensions")

        self.set_operation_context(
            command=command_name,
            phase="store.resolving_item_location",
            item={"barcode": resolved_barcode, "name": resolved_item_name},
        )
        if provided_dimensions is None:
            return RobotCommandError(command=command_name, error="Item dimensions required")

        self._logger.info("Using provided dimensions: %s", provided_dimensions)
        width = float(provided_dimensions.get("width", 0))
        height = float(provided_dimensions.get("height", 0))
        length = float(provided_dimensions.get("length", 0))

        if width <= 0 or height <= 0 or length <= 0:
            return RobotCommandError(command=command_name, error="Invalid dimensions provided")

        weight = await self._io.get_weight()

        item = CellItem(
            dimensions=Dimensions(
                width=width,
                height=height,
                length=length
            ),
            weight=weight,
            data={"name": resolved_item_name},
            barcode=resolved_barcode
        )

        provided_location = None
        if isinstance(item_payload, dict):
            provided_location = item_payload.get("location")

        reserved_locations = getattr(self, "_reserved_cell_locations", set())
        location: StorageModuleCellLocation | None = None
        if provided_location is not None:
            try:
                parsed_location = StorageModuleCellLocation(
                    side=provided_location.get("side"),
                    shelf_idx=int(provided_location.get("shelf_idx", 0)),
                    cell_idx=int(provided_location.get("cell_idx", 0)),
                )
            except (AttributeError, TypeError, ValueError):
                return RobotCommandError(command=command_name, error="Invalid storage location provided")

            is_reserved = (
                parsed_location.side.value,
                parsed_location.shelf_idx,
                parsed_location.cell_idx,
            ) in reserved_locations

            # Demo mode keeps its barcode-to-cell mapping for ordinary items. Reserved bin
            # homes stay trusted explicit destinations for the bin workflow, and a tray
            # return targets a workflow-chosen home rather than a client-requested cell, so
            # the availability and fit checks apply only to ordinary explicit stores.
            if self._demo_cell_map is None or is_reserved:
                if is_reserved is False and tray is False:
                    cell = self._grid.get_cell_if_present(parsed_location)
                    if cell is None:
                        return RobotCommandError(command=command_name, error="Requested storage cell does not exist")
                    if cell.status != CellStatus.AVAILABLE:
                        return RobotCommandError(
                            command=command_name,
                            error=f"Requested storage cell is {cell.status.value}",
                        )
                    if self._grid.cell_fits_item(cell, item) is False:
                        return RobotCommandError(command=command_name, error="Item does not fit the requested storage cell")
                location = parsed_location

        if location is None and self._demo_cell_map is not None:
            demo_location = self._demo_cell_map.get(resolved_barcode)
            if demo_location is None:
                return RobotCommandError(
                    command=command_name,
                    error=f"barcode {resolved_barcode} not in demo mapping",
                )
            if (
                demo_location.side.value,
                demo_location.shelf_idx,
                demo_location.cell_idx,
            ) in reserved_locations:
                return RobotCommandError(
                    command=command_name,
                    error="Demo storage location is reserved",
                )
            location = demo_location
        elif location is None:
            location = self._grid.find_best_slot_for_item(
                item,
                excluded_locations=reserved_locations,
            )

        if not location:
            return RobotCommandError(command=command_name, error="No suitable storage location found")

        self._state.current_cell = location
        self.set_operation_context(
            command=command_name,
            phase="store.moving_from_shelf_to_io",
            target_cell=location,
            item={"barcode": resolved_barcode, "name": resolved_item_name},
        )
        try:
            coord = self._grid.get_cell_coordinate(location)
            self.set_operation_context(target_coordinate=coord.to_axis_coordinate())
            self._logger.info("Store: starting io_pick at %s", location)
            if tray:
                await self.return_tray(coord.to_axis_coordinate())
            else:
                await self.io_pick(coord.to_axis_coordinate())
            self._logger.info("Store: io_pick completed")
            self.set_operation_context(phase="store.grid_update_commit")
            self._grid.store_item(location, item)
            self._logger.info("Store: command successful for %s at %s", resolved_item_name, location)
            return RobotCommandSuccessful(command=command_name, location=location, barcode=resolved_barcode, weight=weight)
        except (MotorError, MovementError, ToolError) as err:
            self._capture_fault_context(err)
            raise
        except Exception as exc:
            self._logger.exception("Store command failed for %s", resolved_item_name)
            sentry_sdk.capture_exception(exc)
            return RobotCommandError(
                command=command_name,
                error=f"{type(exc).__name__}: {exc}"
            )

    async def retrieve(self, item_name: str) -> RobotResponse:
        command_name = "retrieve_item"

        if not self._grid:
            return RobotCommandError(command=command_name, error="Storage grid not initialized")

        location = next(
            (
                entry.location
                for entry in self._grid.get_all_cell_data()
                if entry.cell
                and entry.cell.data
                and isinstance(entry.cell.data, dict)
                and entry.cell.data.get("name") == item_name
            ),
            None,
        )
        if location is None:
            return RobotCommandError(command=command_name, error=f"Item '{item_name}' not found in storage")

        return await self.retrieve_by_location(location.model_dump())

    async def _find_location_for_barcode(self, barcode: str) -> Optional[StorageModuleCellLocation]:
        for entry in self._grid.get_all_cell_data():
            cell = entry.cell

            if cell is None:
                continue

            if cell.barcode == barcode:
                return entry.location

            if isinstance(cell.data, dict) and cell.data.get("barcode") == barcode:
                return entry.location

        return None

    async def retrieve_by_barcode(
        self,
        barcode: str,
        *,
        location_payload: Optional[Dict[str, Any]] = None,
    ) -> RobotResponse:
        command_name = "retrieve_item"

        if not self._grid:
            return RobotCommandError(command=command_name, error="Storage grid not initialized")

        resolved_location: Optional[StorageModuleCellLocation] = None

        if location_payload is not None:
            try:
                resolved_location = StorageModuleCellLocation(**location_payload)
            except Exception:
                return RobotCommandError(command=command_name, error="Invalid location payload")

        if resolved_location is None:
            resolved_location = await self._find_location_for_barcode(barcode)
            if resolved_location is None:
                return RobotCommandError(command=command_name, error=f"Item '{barcode}' not found in storage")

        return await self.retrieve_by_location(resolved_location.model_dump(), barcode_hint=barcode)

    async def retrieve_by_location(
        self,
        location_payload: Dict[str, Any],
        barcode_hint: Optional[str] = None,
        *, tray: bool = False,
    ) -> RobotResponse:
        command_name = "retrieve_item"

        if not self._grid:
            return RobotCommandError(command=command_name, error="Storage grid not initialized")

        self.set_operation_context(command=command_name, phase="retrieve.resolving_cell_location")
        try:
            location = StorageModuleCellLocation(**location_payload)
        except Exception:
            return RobotCommandError(command=command_name, error="Invalid location payload")

        try:
            cell = self._grid.get_cell(location)
        except Exception:
            return RobotCommandError(command=command_name, error="Location out of range")

        cached_item = cell.item
        if cached_item is None and barcode_hint is None:
            self._logger.info(
                "No cached item for location %s/%s/%s; proceeding with retrieval based on command payload",
                location.side,
                location.shelf_idx,
                location.cell_idx,
            )

        self._state.current_cell = location
        # The commanded barcode is the one the server located in this cell; the grid's
        # in-memory item misses location edits made through the API, and reporting it
        # instead would remove the wrong item's location.
        retrieved_barcode: str | None = barcode_hint
        if retrieved_barcode is None and cached_item is not None:
            barcode_from_data = cached_item.data.get("barcode") if cached_item.data else None
            retrieved_barcode = cached_item.barcode or (str(barcode_from_data) if barcode_from_data is not None else None)

        self.set_operation_context(command=command_name, phase="retrieve.moving_from_io_to_shelf", target_cell=location)
        try:
            coord = self._grid.get_cell_coordinate(location).to_axis_coordinate()
            self.set_operation_context(target_coordinate=coord)

            nudge_override = self._get_demo_nudge(location) if self._demo_cell_map is not None else None
            if nudge_override is not None:
                self.set_operation_context(nudge_override=nudge_override)

            if tray:
                await self.deliver_tray(coord)
            else:
                await self.io_place(coord, nudge_override=nudge_override)
            self.set_operation_context(phase="retrieve.grid_clear_commit")
            self._grid.clear_item(location)
            weight = await self._io.get_weight()
            return RobotCommandSuccessful(command=command_name, location=location, barcode=retrieved_barcode, weight=weight)
        except (MotorError, MovementError, ToolError) as err:
            self._capture_fault_context(err)
            raise
        except Exception as exc:
            self._logger.exception(
                "Retrieve command failed at shelf %s cell %s",
                location.shelf_idx,
                location.cell_idx,
            )
            sentry_sdk.capture_exception(exc)
            return RobotCommandError(
                command=command_name,
                error=f"{type(exc).__name__}: {exc}"
            )

    # ------------------------------------------------------------------ #
    #  Command dispatch
    # ------------------------------------------------------------------ #

    async def handle_command(self, command: RobotCommands) -> RobotResponse:
        command_name = getattr(command, "command", "unknown")
        try:
            if isinstance(command, StopCommand):
                await self.stop()
                result: RobotResponse = RobotCommandSuccessful(command=command_name)
            elif isinstance(command, InitializeCommand):
                await self._tool.configure()
                result = RobotCommandSuccessful(command=command_name)
            elif isinstance(command, IdleCommand):
                await self.idle()
                result = RobotCommandSuccessful(command=command_name)
            elif isinstance(command, HomeCommand):
                await self._tool.home()
                await self._io.home_door()
                result = RobotCommandSuccessful(command=command_name)
            elif isinstance(command, MoveCommand):
                self.set_operation_context(command=command_name, phase="move", target_coordinate=command.coordinate)
                await self._tool.move(command.coordinate)
                result = RobotCommandSuccessful(command=command_name)
            elif isinstance(command, ReturnTrayCommand):
                result = await self.store(command.item, tray=True)
            elif isinstance(command, DeliverTrayCommand):
                if command.location is None:
                    result = RobotCommandError(command=command_name, error="Tray home location required")
                else:
                    result = await self.retrieve_by_location(command.location, command.barcode, tray=True)
            elif isinstance(command, StoreItemCommand):
                payload = getattr(command, "barcode", None)
                if payload is None:
                    payload = getattr(command, "item", None)
                result = await self.store(payload)
            elif isinstance(command, RetrieveItemCommand):
                if command.barcode:
                    result = await self.retrieve_by_barcode(command.barcode, location_payload=command.location)
                elif command.location:
                    result = await self.retrieve_by_location(command.location)
                else:
                    result = RobotCommandError(command=command_name, error="Missing barcode or location for retrieval")
            else:
                result = RobotCommandError(command=command_name, error="Invalid Robot Command")
            self.clear_operation_context()
            return result
        except (MotorError, MovementError, ToolError):
            raise
        except Exception as exc:
            self.clear_operation_context()
            self._logger.exception("Unhandled error while processing command %s", command_name)
            sentry_sdk.capture_exception(exc)
            return RobotCommandError(
                command=command_name,
                error=f"{type(exc).__name__}: {exc}"
            )

    # ------------------------------------------------------------------ #
    #  Scale API
    # ------------------------------------------------------------------ #

    def _require_scale(self):
        if self._io._scale is None:
            raise ConfigError("Scale is not enabled")
        return self._io._scale

    async def get_scale_data(self) -> Dict[str, Any]:
        return await self._require_scale().read_weight_data()

    async def get_scale_calibration_status(self) -> Dict[str, Any]:
        return await self._require_scale().get_calibration_status()

    async def get_scale_calibration_params(self) -> Dict[str, Any]:
        return await self._require_scale().get_calibration_params()

    async def set_scale_calibration_params(
        self,
        full_scale: int,
        calibration_weight: int,
        precision: int = 1,
        division: int = 1,
    ) -> bool:
        return await self._require_scale().set_calibration_params(
            full_scale=full_scale,
            calibration_weight=calibration_weight,
            precision=precision,
            division=division,
        )

    async def calibrate_scale_zero(self, timeout: float = 5.0) -> Dict[str, Any]:
        return await self._require_scale().calibrate_zero(timeout=timeout)

    async def calibrate_scale_weight(self, timeout: float = 5.0) -> Dict[str, Any]:
        return await self._require_scale().calibrate_weight(timeout=timeout)

    async def scale_tare(self) -> bool:
        return await self._require_scale().tare()

    # ------------------------------------------------------------------ #
    #  Laser API
    # ------------------------------------------------------------------ #

    def _require_laser(self):
        if self._tool._laser is None or self._tool._laser_enabled is False:
            raise ConfigError("Laser is not enabled")
        return self._tool._laser

    async def get_laser_distance(self) -> float:
        return await self._require_laser().read_distance_mm()

    async def scan_storage(
        self,
        *,
        side: StorageModuleSide | str,
        bounds: ScanBounds,
        step_mm: float,
        settle_ms: int,
        samples_per_point: int,
        laser_offset_x_mm: float,
        laser_offset_y_mm: float,
        wall_depth_tolerance_mm: float = 2.0,
        expected_hole_width_mm: float = 26.0,
        expected_hole_height_mm: float = 30.0,
        hole_width_tolerance_mm: float = 10.0,
        hole_height_tolerance_mm: float = 10.0,
        min_vertical_wall_coverage_mm: float = 10.0,
        min_horizontal_wall_coverage_mm: float = 30.0,
        scans_dir: str = "data/scans",
        apply: bool = True,
        stage_extension_mm: Optional[float] = None,
        z_extension_mm: Optional[float] = None,
        progress_callback: Optional[Any] = None,
    ) -> ScanAnalysisResult:
        if isinstance(side, str):
            side = StorageModuleSide(side)

        side_number = 0 if side == StorageModuleSide.FRONT else 1

        if stage_extension_mm is None:
            stage_param = "STAGE_EXTENSION_SHELF_FRONT" if side == StorageModuleSide.FRONT else "STAGE_EXTENSION_SHELF_BACK"
            stage_value = await self._tool.get_param(stage_param)
            if stage_value is not None:
                stage_extension_mm = float(stage_value)

        x_positions = build_axis_positions(bounds.x_min, bounds.x_max, step_mm)
        y_positions = build_axis_positions(bounds.y_min, bounds.y_max, step_mm)

        depth_rows = await self._tool.capture_laser_scan(
            side=side_number,
            x_positions=x_positions,
            y_positions=y_positions,
            settle_ms=settle_ms,
            samples_per_point=samples_per_point,
            stage_extension_mm=stage_extension_mm,
            z_extension_mm=z_extension_mm,
            progress_callback=progress_callback,
        )

        capture = create_scan_capture(
            side=side,
            bounds=bounds,
            step_mm=step_mm,
            settle_ms=settle_ms,
            samples_per_point=samples_per_point,
            laser_offset_x_mm=laser_offset_x_mm,
            laser_offset_y_mm=laser_offset_y_mm,
            wall_depth_tolerance_mm=wall_depth_tolerance_mm,
            expected_hole_width_mm=expected_hole_width_mm,
            expected_hole_height_mm=expected_hole_height_mm,
            hole_width_tolerance_mm=hole_width_tolerance_mm,
            hole_height_tolerance_mm=hole_height_tolerance_mm,
            min_vertical_wall_coverage_mm=min_vertical_wall_coverage_mm,
            min_horizontal_wall_coverage_mm=min_horizontal_wall_coverage_mm,
            depth_rows=depth_rows,
        )

        analysis = analyze_scan_capture(capture, base_dir=scans_dir)

        if apply is False:
            return analysis

        if len(analysis.shelves) == 0:
            raise ValueError("No tray openings detected from scan")

        existing_shelves = None if self._grid is None else self._grid.get_side(side)
        scanned_shelves = build_shelves_from_scan(
            side=side,
            analysis=analysis,
            existing_shelves=existing_shelves,
        )

        current_front = [] if self._grid is None else self._grid.front_module
        current_back = [] if self._grid is None else self._grid.back_module

        if side == StorageModuleSide.FRONT:
            front_module = scanned_shelves
            back_module = current_back
        else:
            front_module = current_front
            back_module = scanned_shelves

        storage_grid = StorageGrid(
            front_module=front_module,
            back_module=back_module,
            filename=self._grid_path,
        )
        storage_grid.save()
        self._grid = storage_grid

        return analysis

    # ------------------------------------------------------------------ #
    #  Motor diagnostics
    # ------------------------------------------------------------------ #

    def get_motor_names(self) -> Dict[str, List[str]]:
        return {
            "io": ["door"],
            "tool": ["x_axis", "y_axis", "z_axis", "stage", "rotate"],
        }

    def get_motor_info(self) -> Dict[str, List[Dict[str, object]]]:
        def _motor_dict(cfg):
            return {"name": cfg.motor_name, "motor_id": cfg.motor_id}
        return {
            "io": [_motor_dict(m) for m in self._config.robot.io.motors.root if m.enabled],
            "tool": [_motor_dict(m) for m in self._config.robot.tool.motors.root if m.enabled],
        }

    def _get_motor_by_name(self, name: str) -> Optional["Motor"]:
        tool_motors = {
            "x_axis": self._tool._mx,
            "y_axis": self._tool._my,
            "z_axis": self._tool._mg,
            "stage": self._tool._ms,
            "rotate": self._tool._mr,
        }
        io_motors = {
            "door": self._io._md,
        }
        return tool_motors.get(name) or io_motors.get(name)

    def simulate_motor_crash(self, motor_name: str) -> bool:
        motor = self._get_motor_by_name(motor_name)

        if motor is None:
            return False

        motor.set_fault()

        return True

    # ------------------------------------------------------------------ #
    #  Gripper
    # ------------------------------------------------------------------ #

    async def release_gripper(self) -> None:
        await self._tool._gs.close()

    async def grip(self) -> None:
        await self._tool._gs.open()

    # ------------------------------------------------------------------ #
    #  Parameters
    # ------------------------------------------------------------------ #

    def reload_parameters(self) -> None:
        self._tool.reload_parameters()
        self._io.reload_parameters()
