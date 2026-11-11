import asyncio
import json as _json
import logging
import sentry_sdk

from pathlib import Path as _Path
from typing import Any, Callable, Dict, Iterable, Literal, NamedTuple, Optional, List, TYPE_CHECKING

from hardware.controller import SharedModbusController as Controller
from hardware.curtain import MotionSafetyInterlock
from hardware.motor import MotorStatus
from openshelf_os.cabinet import Cabinet
from openshelf_os.common import AxisCoordinate, ConfigError, load_config_file, CONFIG_PATH, resolve_grid_path
from openshelf_os.demo_side_rename import migrate_demo_side_rename
from openshelf_os.config.models import compare_gpio_pins_to_defaults
from openshelf_os.serial_ports import (
    DEFAULT_LASER_PORT,
    DEFAULT_MOTOR_PORT,
    DEFAULT_SCALE_PORT,
    LASER_PORT_CANDIDATES,
    MOTOR_PORT_CANDIDATES,
    SCALE_PORT_CANDIDATES,
)
from openshelf_os.faults import (
    EXECUTION_ERROR_CODE,
    RETRIEVE_FAILED_CODE,
    STORE_FAILED_CODE,
    fault_failure,
)
from openshelf_os.exceptions import (
    ConfigError as CabinetConfigError, FridgeSafetyError, GRIP_DROPPED_CODE, GRIP_IO_BOX_FAILED_CODE,
    MotorError, MovementError, ToolError,
)
from openshelf_os.fridge import DemoFridge
from openshelf_os.iobox import IOBox
from openshelf_os.operating_mode import should_enable_demo_features
from openshelf_os.robot.commands import (
    RobotCommands, StopCommand, InitializeCommand, IdleCommand, HomeCommand, RecoverCommand,
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
from openshelf_os.tool import Tool, require_grip, rewrap_gripper_fault, run_together

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

_INIT_MOTOR_COMPONENTS = [
    "x_axis",
    "y_axis",
    "z_axis",
    "stage",
    "rotate",
    "door",
    "fridge_door",
]

class PowerRail(NamedTuple):
    supplies: str
    #: Motors that lose their incremental encoder reference with the rail.
    axes: tuple[str, ...]
    #: Whether the gripper servo drops too, so it must reconfigure on next use.
    gripper: bool = False


# Switched power rails of the V2.4 board. Asserting a pin CUTS that rail; clearing it --
# the default state -- applies power.
V24_POWER_RAILS: Dict[str, PowerRail] = {
    "pwr_out0": PowerRail(
        "tool drives and gripper",
        ("x_axis", "y_axis", "z_axis", "stage", "rotate"),
        gripper=True,
    ),
    "pwr_out1": PowerRail("IO box", ("door",)),
    "pwr_out2": PowerRail("peripherals", ()),
    "pwr_out3": PowerRail("aux", ()),
}
# N3 (ctrl-v3.0) relays POW_CTRL_X/Y/IO/PERI/AUX1/AUX2, driven straight from Pi pins. Measured
# on lb5-2 2026-11-09: the Y relay drops only the Y drive and the IO relay the IO door and fridge
# door drives; no relay feeds Z, stage, rotate or the gripper. X is listed per the schematic
# even though lb5-2's X relay did not drop its drive, because an unlisted axis would stay homed.
N3_POWER_RAILS: Dict[str, PowerRail] = {
    "pwr_out0": PowerRail("X axis drive", ("x_axis",)),
    "pwr_y": PowerRail("Y axis drive", ("y_axis",)),
    "pwr_out1": PowerRail("IO box", ("door", "fridge_door")),
    "pwr_out2": PowerRail("network switch", ()),
    "pwr_out3": PowerRail("aux 1", ()),
    "pwr_aux2": PowerRail("aux 2", ()),
}


def power_rails_for(gpio_pins: Any) -> Dict[str, PowerRail]:
    """The rail map of the board the pin map describes: N3 drives its relays from Pi pins."""
    relay = gpio_pins.get("pwr_out0") if isinstance(gpio_pins, dict) else None
    if relay is not None and getattr(relay, "board", None) == "rpi":
        return N3_POWER_RAILS
    return V24_POWER_RAILS


POWER_CYCLE_HOLD_S = 3.0
POWER_CYCLE_BOOT_TIMEOUT_S = 20.0
POWER_CYCLE_BOOT_POLL_S = 0.25


STARTUP_HAZARD_CABINET_OPEN = "the cabinet doors were open"
STARTUP_HOME_SKIPPED_CABINET_OPEN = f"Startup homing skipped because {STARTUP_HAZARD_CABINET_OPEN}"
STARTUP_SAFETY_POLL_S = 0.05
STARTUP_HOME_SKIPPED_TRAY_ON_TOOL = "Startup homing skipped because the gripper holds a tray"
# Tail of the refusal for a barcode missing from the demo cell map; callers match on it, so edit it here only.
DEMO_MAP_MISSING_SUFFIX = " is not in the demo cell map."


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
        self._fridge: DemoFridge | None = None
        fridge_motor = self._config.demo.fridge.motor
        if (
            self._config.demo.fridge.enabled is True
            and fridge_motor is not None
            and fridge_motor.enabled is True
        ):
            self._fridge = DemoFridge(
                motorcontroller=self._motorbus,
                params=self._config.demo.fridge,
                safety=self._motion_safety,
                is_fully_retracted=self._tool.is_fully_retracted,
                is_rotated_back=self._tool.is_rotated_back,
                retract_tool=self._tool.retract_empty,
                x_axis_middle_mm=self._tool.x_axis_travel_middle_mm,
                gpio=self._gpio,
            )

        self._grid_path = resolve_grid_path(self._config, CONFIG_PATH)
        self._reserved_cell_locations: set[tuple[str, int, int]] = {
            (entry.home_location.side, entry.home_location.shelf_idx, entry.home_location.cell_idx)
            for entry in self._config.storage.bins
        }

        try:
            self._grid = StorageGrid.load_from_file(self._grid_path)
        except StorageValidationError as e:
            self._logger.warning("storage_load_failed: %s - %s", self._grid_path, str(e), exc_info=e)
            self._grid = StorageGrid()

        self._init_reporter: Optional[InitProgressReporter] = None
        self._failed_init_components: set[str] = set()

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
        # Remembered so a startup safety trip does not relabel a failed component as ready.
        if phase == "failed":
            self._failed_init_components.add(component)
        else:
            self._failed_init_components.discard(component)
        reporter = getattr(self, "_init_reporter", None)
        if reporter is None:
            return
        try:
            reporter(component, phase, detail, reason)
        except Exception:
            pass

    def set_init_progress_reporter(self, reporter: Optional[InitProgressReporter]) -> None:
        self._init_reporter = reporter
        self._tool._init_reporter = self._emit_init
        self._io._init_reporter = reporter
        fridge = getattr(self, "_fridge", None)
        if fridge is not None:
            fridge._init_reporter = reporter

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

    async def configure(self, isolate: bool = False, home_on_init: bool = True) -> Optional[str]:
        """Connect and configure the hardware, homing it if asked.

        Returns why a requested startup home was skipped or stopped, or None.
        """
        startup_home_skip_reason: Optional[str] = None
        self._failed_init_components = set()
        self._emit_init("motor_bus", "connecting")
        try:
            await self._connect_controller("motor", self._motorbus, self._bus_settings["motor"])
        except Exception:
            self._emit_init("motor_bus", "failed", reason="The motor bus did not connect. Check its cable and power.")
            for name in _INIT_MOTOR_COMPONENTS:
                if self._get_motor_by_name(name) is not None:
                    self._emit_init(name, "failed", reason="The motor bus is not connected.")
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

        # The pre-home clearance jog of the startup home runs before the worker
        # exists, so the guard has to start sampling here or the boot-time crush
        # scenario goes unguarded. The worker replaces the listener when it
        # connects; start() is idempotent so its later start is a no-op.
        weight_guard = getattr(self, "_weight_guard", None)
        if weight_guard is not None:
            weight_guard.set_listener(self._stop_motion_on_weight_change)
            await weight_guard.start()

        # Homing waits until every sensor is live, so the startup home is watched by
        # the same inputs that guard motion once the worker runs.
        await self._tool.configure(isolate=isolate, home_on_init=False)
        await self._io.configure(isolate=isolate, scale_bus_ready=scale_bus_ready, home_on_init=False)
        cabinet = getattr(self, "_cabinet", None)
        if cabinet is not None:
            self._emit_init("cabinet", "configuring")
            try:
                await cabinet.configure()
                self._emit_init("cabinet", "ready")
            except Exception as exc:
                self._emit_init("cabinet", "failed", reason="The cabinet did not start. Check its connection.")
                if isolate is False:
                    raise
                self._logger.warning("Cabinet configure failed", exc_info=exc)

        fridge = getattr(self, "_fridge", None)
        if fridge is not None:
            await fridge.configure(isolate=isolate, home_on_init=False)

        if home_on_init is True:
            if self._tool.is_tray_held is True:
                # The tool may still be in a cell or the IO box, so the door stays put as well.
                startup_home_skip_reason = STARTUP_HOME_SKIPPED_TRAY_ON_TOOL
            else:
                startup_home_skip_reason = await self._run_startup_home(isolate)

        for name in _INIT_MOTOR_COMPONENTS:
            motor = self._get_motor_by_name(name)
            if motor is not None:
                motor._on_move_complete = self._did_complete_motor_move

        return startup_home_skip_reason

    async def _run_startup_home(self, isolate: bool) -> Optional[str]:
        """Home every axis unless a safety input is tripped before or during it.

        The worker's guards and e-stop monitor only start after configure returns, so
        this watch is what stops the drives here. A trip blocks the interlock (moves
        pause at their next poll), abandons the home and leaves the interlock blocked
        for the worker to release once its own guards agree. Returns why the home was
        skipped or stopped, or None when it completed.
        """
        hazard = self._startup_motion_hazard()
        if hazard is not None:
            self.block_motion_for_safety()
            self._logger.warning("Startup homing skipped: %s", hazard)
            return f"Startup homing skipped because {hazard}"

        home = asyncio.create_task(self._startup_home_sequence(isolate), name="startup-home")
        watch = asyncio.create_task(self._watch_startup_hazards(), name="startup-safety-watch")
        try:
            await asyncio.wait({home, watch}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (home, watch):
                if task.done() is False:
                    task.cancel()
            await asyncio.gather(home, watch, return_exceptions=True)

        if watch.cancelled() is True:
            home.result()
            return None

        hazard = watch.result()
        # The cancelled home may have left a drive mid-move between interlock polls.
        stops = [self._tool.stop, self._io.stop_door]
        fridge = getattr(self, "_fridge", None)
        if fridge is not None:
            stops.append(fridge.stop)
        for stop in stops:
            try:
                await stop()
            except Exception as exc:
                self._logger.warning("Stop after a startup safety trip failed", exc_info=exc)
        self._tool.mark_configured_motors_ready(self._failed_init_components)
        self._io.mark_door_ready()
        self._logger.warning("Startup homing stopped: %s", hazard)
        return f"Startup homing stopped because {hazard}"

    async def _startup_home_sequence(self, isolate: bool) -> None:
        await self._tool.startup_home(isolate=isolate)
        try:
            await self._io.home_door()
        except Exception as exc:
            if isolate is False:
                raise
            self._logger.warning("init iobox home_door failed: %s", exc, exc_info=exc)
        fridge = getattr(self, "_fridge", None)
        if fridge is None:
            return
        # Last, as before the guarded startup home: the fridge door needs the tool retracted.
        try:
            await fridge.home_door()
        except Exception as exc:
            if isolate is False:
                raise
            self._logger.warning("init fridge home_door failed", exc_info=exc)

    async def _watch_startup_hazards(self) -> str:
        while True:
            hazard = self._startup_motion_hazard()
            if hazard is not None:
                self.block_motion_for_safety()
                return hazard
            await asyncio.sleep(STARTUP_SAFETY_POLL_S)

    def _startup_motion_hazard(self) -> Optional[str]:
        """Why motion must not start or continue before the worker runs, or None."""
        if self._motion_safety.blocked is True:
            # Only the weight guard blocks the interlock before the worker exists.
            return "the weight guard tripped"

        try:
            if self._is_emergency_stop_pressed() is True:
                return "the emergency stop was pressed"
        except Exception as exc:
            self._logger.warning("Emergency stop read failed before startup homing", exc_info=exc)
            return "the emergency stop could not be read"

        if self._are_cabinet_doors_open_at_startup() is True:
            return STARTUP_HAZARD_CABINET_OPEN

        if self._io.get_curtain_state() is True and self._io.get_door_state() is True:
            return "the light curtain was blocked with the IO door open"

        return None

    def _is_emergency_stop_pressed(self) -> bool:
        estop_config = self._config.get("robot.emergency_stop", None)
        if estop_config is not None and getattr(estop_config, "enabled", True) is False:
            return False

        gpio = getattr(self, "_gpio", None)
        if gpio is None or gpio.has_pin("estop") is False:
            return False

        return gpio.input("estop").read() is True

    def _are_cabinet_doors_open_at_startup(self) -> bool:
        cabinet = getattr(self, "_cabinet", None)
        if cabinet is None or cabinet.is_configured() is False:
            return False

        try:
            return cabinet.get_door_state()["open"] is True
        except Exception:
            self._logger.warning("Cabinet door read failed before startup homing", exc_info=True)
            return True

    async def _connect_optional_bus(self, label: str, controller: Controller, isolate: bool) -> bool:
        self._emit_init(f"{label}_bus", "connecting")
        try:
            await self._connect_controller(label, controller, self._bus_settings[label])
            self._emit_init(f"{label}_bus", "ready")
            return True
        except Exception as exc:
            self._emit_init(f"{label}_bus", "failed", reason=f"The {label} bus did not connect. Check its cable and power.")
            if isolate is False:
                raise
            self._logger.warning("%s bus connect failed", label, exc_info=exc)
            return False

    async def shutdown(self) -> None:
        await self.stop()

        motors = [
            self._tool._mx,
            self._tool._my,
            self._tool._mg,
            self._tool._ms,
            self._tool._mr,
            self._io._md,
        ]
        fridge = getattr(self, "_fridge", None)
        if fridge is not None:
            motors.append(fridge._door)
        for motor in motors:
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
        fridge = getattr(self, "_fridge", None)
        if fridge is not None:
            await fridge.stop()
        cabinet = getattr(self, "_cabinet", None)
        if cabinet is not None:
            await cabinet.stop()

    async def stop_motion(self) -> None:
        """Halt every motor. Unlike stop(), the shutdown, the curtain and cabinet monitors keep running."""
        try:
            await self._tool.stop()
        finally:
            try:
                await self._io.stop_door()
            finally:
                fridge = getattr(self, "_fridge", None)
                if fridge is not None:
                    await fridge.stop()

    def get_power_rail_states(self) -> Dict[str, Optional[bool]]:
        """Whether each rail is currently powered. None if the pin is unavailable.

        The pin is asserted to CUT power, so powered is its inverse.
        """
        gpio = getattr(self, "_gpio", None)
        states: Dict[str, Optional[bool]] = {}

        for rail in self.power_rails():
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
                    extra={"motor_name": name, "error": str(e)}, exc_info=e)
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
        rails = self.power_rails()
        if rail not in rails:
            raise ConfigError(f"Unknown power rail '{rail}'")

        spec = rails[rail]
        axes = self.power_rail_axes(rail)

        for name in axes:
            motor = self._get_motor_by_name(name)
            if motor is not None and motor._status is MotorStatus.MOVING:
                raise MotorError(f"Refusing to power cycle {rail} while {name} is moving")

        gpio = getattr(self, "_gpio", None)
        if gpio is None or gpio.has_pin(rail) is False:
            raise ConfigError(f"GPIO pin {rail} is not configured")

        for name in axes:
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
                await self._reconfigure_after_power_cycle(axes)
            except MotorError as exc:
                # _reconfigure_after_power_cycle already logged per-axis failure reasons.
                configure_error = exc

        if configure_error is not None:
            raise configure_error

        return axes

    def power_rails(self) -> Dict[str, PowerRail]:
        config = getattr(self, "_config", None)
        gpio_config = getattr(getattr(config, "robot", None), "gpio", None)
        return power_rails_for(getattr(gpio_config, "pins", None))

    def power_rail_axes(self, rail: str) -> tuple[str, ...]:
        """The rail's axes that this machine has; a cut only owes a home for a fitted motor."""
        return tuple(name for name in self.power_rails()[rail].axes if self._get_motor_by_name(name) is not None)

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
        axes = ["x_axis", "y_axis", "z_axis", "stage", "rotate", "door"]
        if getattr(self, "_fridge", None) is not None:
            axes.append("fridge_door")
        for axis in axes:
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
        # Demo mapping files written before the front/back cutover key their cells under the
        # L/R side letters; the shared migration renames them in place, as it does for the
        # other demo.json readers (scripts/seed_demo.py).
        raw = migrate_demo_side_rename(str(file_path), raw_content, raw)
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

    def _blocked_fridge_cell_error(
        self, command_name: str, location: StorageModuleCellLocation
    ) -> RobotCommandError | None:
        """Refuse a configured fridge cell while the fridge is off: its door stays closed."""
        config = getattr(self, "_config", None)
        if getattr(self, "_fridge", None) is not None or config is None:
            return None
        cells = config.demo.fridge.cells
        key = (location.side.value, location.shelf_idx, location.cell_idx)
        if any((c.location.side, c.location.shelf_idx, c.location.cell_idx) == key for c in cells):
            return RobotCommandError(
                command=command_name,
                error="This cell is behind the demo fridge door, and the fridge is disabled",
            )
        return None

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
                    side, shelf_idx, cell_idx, str(e), exc_info=e)

    def set_reserved_cell_locations(self, locations: Iterable[tuple[str, int, int]]) -> None:
        """Replace the bin homes that ordinary stores and allocation must avoid.

        The caller passes the complete set; the robot's own config is the startup copy and
        would keep a since-removed bin's home reserved.
        """
        self._reserved_cell_locations = set(locations)


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
        motors = {
            "x_axis": gantry_status.x_axis,
            "y_axis": gantry_status.y_axis,
            "z_axis": tool_status.z_axis,
            "stage": tool_status.stage or AxisTelemetry(),
            "rotate": tool_status.rotate or AxisTelemetry(),
            "door": await self._build_axis_status(getattr(self._io, "_md", None)),
        }
        fridge = getattr(self, "_fridge", None)
        if fridge is not None:
            motors["fridge_door"] = await self._build_axis_status(fridge._door)

        status = RobotStatus(
            gantry=gantry_status,
            tool=tool_status,
            state=state,
            storage=self._grid,
            cabinet_doors=cabinet_doors,
            io_door_open=self.get_door_state(),
            motors=motors,
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

    async def _access_storage_cell(
        self,
        operation: Literal["pick", "place"],
        coord: AxisCoordinate,
        location: StorageModuleCellLocation | None,
        *,
        loaded: bool,
    ) -> None:
        fridge = getattr(self, "_fridge", None)
        cell_settings = (
            fridge.cell_settings(location)
            if fridge is not None and location is not None
            else None
        )
        tool_operation = self._tool.pick if operation == "pick" else self._tool.place

        if fridge is None or cell_settings is None or location is None:
            await tool_operation(coord, loaded=loaded)
            return

        door_may_be_open = False

        async def open_door() -> None:
            nonlocal door_may_be_open
            direction = fridge.direction_for_cell_x(coord.x)
            self.set_operation_context(
                phase=f"fridge.{operation}.open_{direction}_door",
                target_cell=location,
            )
            door_may_be_open = True
            await fridge.open_for_cell(location, cell_x_mm=coord.x)

        async def close_door() -> None:
            nonlocal door_may_be_open
            self.set_operation_context(
                phase=f"fridge.{operation}.close_door",
                target_cell=location,
            )
            await fridge.close_door()
            door_may_be_open = False

        try:
            await tool_operation(
                coord,
                loaded=loaded,
                stage_extension_mm=cell_settings.stage_extension_mm,
                before_approach=open_door,
                after_retract=close_door,
            )
        except Exception:
            retraction_verified = False
            if door_may_be_open is True:
                retraction_verified = await self._tool.is_fully_retracted()
            if door_may_be_open is True and retraction_verified is True:
                try:
                    await fridge.close_door()
                    door_may_be_open = False
                except Exception as close_error:
                    self._logger.warning(
                        "Failed to close fridge door during safe recovery",
                        extra={"error": str(close_error)},
                    )

            if door_may_be_open is True:
                self._logger.error(
                    "Fridge door remains open after failed storage access",
                    extra={
                        "operation": operation,
                        "location": self._safe_model_dump(location),
                        "tool_retraction_verified": retraction_verified,
                    },
                )
            raise

    async def io_place(
        self,
        coord: AxisCoordinate,
        nudge_override: float | None = None,
        location: StorageModuleCellLocation | None = None,
    ) -> None:
        ppl = await self._tool.get_param("PICKUP_POSITION_L")
        pph = await self._tool.get_param("PICKUP_POSITION_H")
        if ppl is None or pph is None:
            raise ConfigError("Missing PICKUP_POSITION_L or PICKUP_POSITION_H parameter")

        if self._tool._my is None:
            raise ConfigError("Y-axis motor not configured")

        self.set_operation_context(phase="io_place.ensure_door_homed")
        await self._io.ensure_door_homed()
        self.set_operation_context(phase="io_place.pick_from_shelf", target_coordinate=coord)
        await self._access_storage_cell("pick", coord, location, loaded=True)
        self.set_operation_context(phase="io_place.rotate_to_front")
        await self._tool.rotate_to_position("front")
        # The door opens while the gantry approaches. The phase keeps its open_door name
        # for the whole step: the light curtain blocks motion during an opening door.
        self.set_operation_context(phase="io_place.open_door")
        await run_together(self._tool.move(AxisCoordinate(ppl[0], ppl[1])), self._io.open_door())
        self.set_operation_context(phase="io_place.extend_into_io", nudge_override=nudge_override)
        await self._tool.extend_IO(nudge_override=nudge_override, loaded=True)
        self.set_operation_context(phase="io_place.y_axis_transfer")
        await self._tool._my.move_to_position(pph[1])
        self.set_operation_context(phase="io_place.retract")
        # The item stays in the IO box, so the empty tray may leave on both axes at once.
        await self._tool.retract(loaded=False)
        # A tray left in the IO box would meet the closing door.
        await self._require_tray_held("io place retract", GRIP_IO_BOX_FAILED_CODE)
        # The tool is clear of the IO box, so the door closes while the gantry returns.
        self.set_operation_context(phase="io_place.close_door")
        await run_together(self._io.close_door(), self._tool.move(coord))
        # The closed door re-homes while the tool places; neither crosses the other's path.
        self.set_operation_context(phase="io_place.place_at_shelf_coordinate")
        await run_together(
            self._access_storage_cell("place", coord, location, loaded=False),
            self._io.home_door_after_cycle(),
        )
        self.set_operation_context(phase="io_place.clear_alarms")
        await self._tool.clear_alarms()

    async def io_pick(
        self,
        coord: AxisCoordinate,
        location: StorageModuleCellLocation | None = None,
    ) -> None:
        ppl = await self._tool.get_param("PICKUP_POSITION_L")
        pph = await self._tool.get_param("PICKUP_POSITION_H")
        if ppl is None or pph is None:
            raise ConfigError("Missing PICKUP_POSITION_L or PICKUP_POSITION_H parameter")

        if self._tool._my is None:
            raise ConfigError("Y-axis motor not configured")

        self.set_operation_context(phase="io_pick.ensure_door_homed")
        await self._io.ensure_door_homed()
        self.set_operation_context(phase="io_pick.pick_from_shelf_cell", target_coordinate=coord)
        # A store targets an available cell, so its tray comes off the shelf empty.
        await self._access_storage_cell("pick", coord, location, loaded=False)
        self.set_operation_context(phase="io_pick.rotate_to_front")
        await self._tool.rotate_to_position("front")
        self.set_operation_context(phase="io_pick.open_door")
        await run_together(self._tool.move(AxisCoordinate(pph[0], pph[1])), self._io.open_door())
        self.set_operation_context(phase="io_pick.extend_into_io")
        await self._tool.extend(loaded=False)
        self.set_operation_context(phase="io_pick.y_axis_transfer")
        await self._tool._my.move_to_position(ppl[1])
        self.set_operation_context(phase="io_pick.retract")
        await self._tool.retract(loaded=True)
        await self._require_tray_held("io pick retract", GRIP_IO_BOX_FAILED_CODE)
        self.set_operation_context(phase="io_pick.close_door")
        await run_together(self._io.close_door(), self._tool.move(coord))
        self.set_operation_context(phase="io_pick.place_back_at_shelf_coordinate")
        await run_together(
            self._access_storage_cell("place", coord, location, loaded=True),
            self._io.home_door_after_cycle(),
        )
        self.set_operation_context(phase="io_pick.clear_alarms")
        await self._tool.clear_alarms()

    async def _require_tray_held(self, phase: str, failure_code: str) -> None:
        """Raise unless the tool still holds the tray it is carrying."""
        if self._tool.is_grip_detection_enabled is False:
            return
        try:
            require_grip(await self._tool._gs.read_state(), phase, failure_code=failure_code)
        except Exception as exc:
            raise rewrap_gripper_fault(exc, "Tray grip verification failed") from exc

    async def _set_tray_grip(self, *, engaged: bool) -> None:
        """Route tray servo failures through the command hardware-fault interlock.

        The tray sits in the IO box here, so a failed grip check leaves it there: the operator
        has to clear the IO box, and the door has to home again with the tool.
        """
        try:
            if engaged:
                require_grip(
                    await self._tool._gs.open(),
                    "bin grip",
                    is_enabled=self._tool.is_grip_detection_enabled,
                    failure_code=GRIP_IO_BOX_FAILED_CODE,
                )
            else:
                await self._tool.release("bin release")
        except Exception as exc:
            action = "grip" if engaged else "release"
            raise rewrap_gripper_fault(exc, f"Bin {action} failed") from exc
    async def deliver_tray(self, coord: AxisCoordinate) -> None:
        """Leave the entire tray supported in the IO box, with the tool clear."""
        position = await self._tool.get_param("PICKUP_POSITION_MULTI_ITEM")
        if position is None:
            raise ConfigError("Missing PICKUP_POSITION_MULTI_ITEM parameter")
        self.set_operation_context(phase="deliver_tray.ensure_door_homed")
        await self._io.ensure_door_homed()
        self.set_operation_context(phase="deliver_tray.pick_from_shelf", target_coordinate=coord)
        await self._tool.pick(coord, loaded=True)
        self.set_operation_context(phase="deliver_tray.rotate_to_front")
        await self._tool.rotate_to_position("front")
        self.set_operation_context(phase="deliver_tray.move_to_io")
        await self._tool.move(AxisCoordinate(position[0], position[1]))
        # A tray dropped on the way must not be reported as delivered; the door is still closed.
        await self._require_tray_held("bin delivery", GRIP_DROPPED_CODE)
        self.set_operation_context(phase="deliver_tray.open_door")
        await self._io.open_door()
        self.set_operation_context(phase="deliver_tray.extend_into_io")
        # Use the same supported pose for delivery and collection. No item-transfer
        # Y motion: the tray itself stays here instead of lifting off its contents.
        await self._tool.extend_IO_multi_item(loaded=True)
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
        position = await self._tool.get_param("PICKUP_POSITION_MULTI_ITEM")
        if position is None:
            raise ConfigError("Missing PICKUP_POSITION_MULTI_ITEM parameter")
        self.set_operation_context(phase="return_tray.ensure_door_homed")
        await self._io.ensure_door_homed()
        self.set_operation_context(phase="return_tray.release")
        await self._set_tray_grip(engaged=False)
        self.set_operation_context(phase="return_tray.rotate_to_front")
        await self._tool.rotate_to_position("front")
        self.set_operation_context(phase="return_tray.move_to_io")
        await self._tool.move(AxisCoordinate(position[0], position[1]))
        self.set_operation_context(phase="return_tray.open_door")
        await self._io.open_door()
        self.set_operation_context(phase="return_tray.extend_into_io")
        # The gripper is released, so the empty tool reaches the tray on both axes at once.
        await self._tool.extend_IO_multi_item(loaded=False)
        self.set_operation_context(phase="return_tray.grip")
        await self._set_tray_grip(engaged=True)
        self.set_operation_context(phase="return_tray.retract")
        await self._tool.retract(loaded=True)
        await self._require_tray_held("bin retract", GRIP_IO_BOX_FAILED_CODE)
        self.set_operation_context(phase="return_tray.close_door")
        await self._io.close_door()
        self.set_operation_context(phase="return_tray.place_at_home", target_coordinate=coord)
        await self._tool.place(coord, loaded=True)
        self.set_operation_context(phase="return_tray.home_door")
        await self._io.home_door()
        await self._tool.clear_alarms()

    # ------------------------------------------------------------------ #
    #  Store / retrieve
    # ------------------------------------------------------------------ #

    async def store(self, item_payload: Any, *, tray: bool = False) -> RobotResponse:
        command_name = "store_item"

        if not self._grid:
            return RobotCommandError(command=command_name, error="The machine's storage is not ready. Reset it and try again.")

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
            return RobotCommandError(command=command_name, error="This item needs its size before it can be stored.")

        self._logger.info("Using provided dimensions: %s", provided_dimensions)
        width = float(provided_dimensions.get("width", 0))
        height = float(provided_dimensions.get("height", 0))
        length = float(provided_dimensions.get("length", 0))

        if width <= 0 or height <= 0 or length <= 0:
            return RobotCommandError(command=command_name, error="Check the item size and try again.")

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
                return RobotCommandError(command=command_name, error="That cell is not valid.")

            is_reserved = (
                parsed_location.side.value,
                parsed_location.shelf_idx,
                parsed_location.cell_idx,
            ) in reserved_locations

            # A tray home holds its tray, so only that tray's return may target it; the cell's
            # status does not mark it.
            if is_reserved and not tray:
                self._logger.info("Store refused: cell %s is a bin home", parsed_location)
                return RobotCommandError(command=command_name, error="That cell is not available.")

            # Demo mode keeps its barcode-to-cell mapping for ordinary items. A tray return
            # targets a workflow-chosen home rather than a client-requested cell, so the
            # availability and fit checks apply only to ordinary explicit stores.
            if self._demo_cell_map is None or is_reserved:
                if is_reserved is False and tray is False:
                    cell = self._grid.get_cell_if_present(parsed_location)
                    if cell is None:
                        return RobotCommandError(command=command_name, error="That cell does not exist.")
                    if cell.status != CellStatus.AVAILABLE:
                        self._logger.info("Store refused: cell %s is %s", parsed_location, cell.status.value)
                        return RobotCommandError(command=command_name, error="That cell is not available.")
                    if self._grid.cell_fits_item(cell, item) is False:
                        return RobotCommandError(command=command_name, error="The item does not fit that cell.")
                location = parsed_location

        if location is None and self._demo_cell_map is not None:
            demo_location = self._demo_cell_map.get(resolved_barcode)
            if demo_location is None:
                return RobotCommandError(
                    command=command_name,
                    error=f"Barcode {resolved_barcode}{DEMO_MAP_MISSING_SUFFIX}",
                )
            if (
                demo_location.side.value,
                demo_location.shelf_idx,
                demo_location.cell_idx,
            ) in reserved_locations:
                return RobotCommandError(
                    command=command_name,
                    error="That cell is reserved for the demo.",
                )
            location = demo_location
        elif location is None:
            location = self._grid.find_best_slot_for_item(
                item,
                excluded_locations=reserved_locations,
            )

        if not location:
            return RobotCommandError(command=command_name, error="No open cell fits this item.")

        if tray is False and (blocked := self._blocked_fridge_cell_error(command_name, location)) is not None:
            return blocked

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
            fridge = getattr(self, "_fridge", None)
            if tray:
                await self.return_tray(coord.to_axis_coordinate())
            elif fridge is not None and fridge.is_fridge_cell(location):
                await self.io_pick(coord.to_axis_coordinate(), location=location)
            else:
                await self.io_pick(coord.to_axis_coordinate())
            self._logger.info("Store: io_pick completed")
            self.set_operation_context(phase="store.grid_update_commit")
            # A tray home is a reservation, not inventory: its members are the items, so the
            # tray's return leaves the home cell's status and item untouched.
            if not tray:
                self._grid.store_item(location, item)
            self._logger.info("Store: command successful for %s at %s", resolved_item_name, location)
            return RobotCommandSuccessful(command=command_name, location=location, barcode=resolved_barcode, weight=weight)
        except (MotorError, MovementError, ToolError) as err:
            self._capture_fault_context(err)
            raise
        except FridgeSafetyError:
            raise
        except Exception as exc:
            self._logger.exception("Store command failed for %s", resolved_item_name)
            sentry_sdk.capture_exception(exc)
            cause, message = fault_failure(exc, STORE_FAILED_CODE)
            return RobotCommandError(
                command=command_name,
                error=message,
                fault_code=cause,
            )

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
            return RobotCommandError(command=command_name, error="The machine's storage is not ready. Reset it and try again.")

        resolved_location: Optional[StorageModuleCellLocation] = None

        if location_payload is not None:
            try:
                resolved_location = StorageModuleCellLocation(**location_payload)
            except Exception:
                return RobotCommandError(command=command_name, error="That cell is not valid.")

        if resolved_location is None:
            resolved_location = await self._find_location_for_barcode(barcode)
            if resolved_location is None:
                return RobotCommandError(command=command_name, error=f"Item {barcode} is not in storage.")

        return await self.retrieve_by_location(resolved_location.model_dump(), barcode_hint=barcode)

    async def retrieve_by_location(
        self,
        location_payload: Dict[str, Any],
        barcode_hint: Optional[str] = None,
        *, tray: bool = False,
    ) -> RobotResponse:
        command_name = "retrieve_item"

        if not self._grid:
            return RobotCommandError(command=command_name, error="The machine's storage is not ready. Reset it and try again.")

        self.set_operation_context(command=command_name, phase="retrieve.resolving_cell_location")
        try:
            location = StorageModuleCellLocation(**location_payload)
        except Exception:
            return RobotCommandError(command=command_name, error="That cell is not valid.")

        try:
            cell = self._grid.get_cell(location)
        except Exception:
            return RobotCommandError(command=command_name, error="That cell is not valid.")

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

        if tray is False and (blocked := self._blocked_fridge_cell_error(command_name, location)) is not None:
            return blocked

        self.set_operation_context(command=command_name, phase="retrieve.moving_from_io_to_shelf", target_cell=location)
        try:
            coord = self._grid.get_cell_coordinate(location).to_axis_coordinate()
            self.set_operation_context(target_coordinate=coord)

            nudge_override = self._get_demo_nudge(location) if self._demo_cell_map is not None else None
            if nudge_override is not None:
                self.set_operation_context(nudge_override=nudge_override)

            fridge = getattr(self, "_fridge", None)
            if tray:
                await self.deliver_tray(coord)
            elif fridge is not None and fridge.is_fridge_cell(location):
                await self.io_place(coord, nudge_override=nudge_override, location=location)
            else:
                await self.io_place(coord, nudge_override=nudge_override)
            self.set_operation_context(phase="retrieve.grid_clear_commit")
            self._grid.clear_item(location)
            weight = await self._io.get_weight()
            return RobotCommandSuccessful(command=command_name, location=location, barcode=retrieved_barcode, weight=weight)
        except (MotorError, MovementError, ToolError) as err:
            self._capture_fault_context(err)
            raise
        except FridgeSafetyError:
            raise
        except Exception as exc:
            self._logger.exception(
                "Retrieve command failed at shelf %s cell %s",
                location.shelf_idx,
                location.cell_idx,
            )
            sentry_sdk.capture_exception(exc)
            cause, message = fault_failure(exc, RETRIEVE_FAILED_CODE)
            return RobotCommandError(
                command=command_name,
                error=message,
                fault_code=cause,
            )

    # ------------------------------------------------------------------ #
    #  Command dispatch
    # ------------------------------------------------------------------ #

    async def handle_command(self, command: RobotCommands) -> RobotResponse:
        command_name = getattr(command, "command", "unknown")
        try:
            if isinstance(command, StopCommand):
                # A queued stop halts motion; stop() is the shutdown teardown and would
                # also end the curtain and cabinet monitors, which nothing restarts.
                await self.stop_motion()
                result: RobotResponse = RobotCommandSuccessful(command=command_name)
            elif isinstance(command, InitializeCommand):
                await self._tool.configure()
                result = RobotCommandSuccessful(command=command_name)
            elif isinstance(command, IdleCommand):
                await self.idle()
                result = RobotCommandSuccessful(command=command_name)
            elif isinstance(command, HomeCommand):
                await self._tool.home(axes=command.axes)
                if command.axes is None or "door" in command.axes:
                    await self._io.home_door()
                fridge = getattr(self, "_fridge", None)
                if fridge is not None and (command.axes is None or "fridge_door" in command.axes):
                    await fridge.home_door()
                result = RobotCommandSuccessful(command=command_name)
            elif isinstance(command, RecoverCommand):
                await self.recover(command.axes)
                result = RobotCommandSuccessful(command=command_name)
            elif isinstance(command, MoveCommand):
                self.set_operation_context(command=command_name, phase="move", target_coordinate=command.coordinate)
                await self._tool.move(command.coordinate)
                result = RobotCommandSuccessful(command=command_name)
            elif isinstance(command, ReturnTrayCommand):
                result = await self.store(command.item, tray=True)
            elif isinstance(command, DeliverTrayCommand):
                if command.location is None:
                    result = RobotCommandError(command=command_name, error="The bin's cell is missing.")
                else:
                    result = await self.retrieve_by_location(command.location, command.barcode, tray=True)
            elif isinstance(command, StoreItemCommand):
                result = await self.store(command.item)
            elif isinstance(command, RetrieveItemCommand):
                if command.barcode:
                    result = await self.retrieve_by_barcode(command.barcode, location_payload=command.location)
                elif command.location:
                    result = await self.retrieve_by_location(command.location)
                else:
                    result = RobotCommandError(command=command_name, error="This item has no cell to retrieve from.")
            else:
                self._logger.warning("Unknown robot command %s", command_name)
                result = RobotCommandError(command=command_name, error="That robot command does not exist.")
            self.clear_operation_context()
            return result
        except (FridgeSafetyError, MotorError, MovementError, ToolError):
            raise
        except Exception as exc:
            self.clear_operation_context()
            self._logger.exception("Unhandled error while processing command %s", command_name)
            sentry_sdk.capture_exception(exc)
            cause, message = fault_failure(exc, EXECUTION_ERROR_CODE)
            return RobotCommandError(
                command=command_name,
                error=message,
                fault_code=cause,
            )

    # ------------------------------------------------------------------ #
    #  Scale API
    # ------------------------------------------------------------------ #

    def _require_scale(self):
        if self._io._scale is None:
            raise ConfigError("The scale is not available on this machine.")
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

    # ------------------------------------------------------------------ #
    #  Motor diagnostics
    # ------------------------------------------------------------------ #

    def get_motor_names(self) -> Dict[str, List[str]]:
        names = {
            "io": ["door"],
            "tool": ["x_axis", "y_axis", "z_axis", "stage", "rotate"],
        }
        if getattr(self, "_fridge", None) is not None:
            names["io"].append("fridge_door")
        return names

    def get_motor_info(self) -> Dict[str, List[Dict[str, object]]]:
        def _motor_dict(cfg):
            return {"name": cfg.motor_name, "motor_id": cfg.motor_id}
        io_motors = [_motor_dict(m) for m in self._config.robot.io.motors.root if m.enabled]
        fridge = getattr(self, "_fridge", None)
        if fridge is not None:
            io_motors.append(_motor_dict(fridge._door._params))
        return {
            "io": io_motors,
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
            "fridge_door": self._fridge._door if getattr(self, "_fridge", None) is not None else None,
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

    async def recover(self, axes: Optional[List[str]] = None) -> None:
        """Withdraw the tool after a grip fault without homing what kept its position.

        The fingers open first: a failed grip can leave them touching the tray or the cell, and
        withdrawing would drag it. The door closes through its home, as at the end of every IO
        cycle, only when it is open, its last home did not finish, or it is in `axes`. The other
        `axes` lost their position for another reason and home once the tool is out.
        """
        await self.release_gripper()
        await self._tool.retract_empty()
        if self._io.get_door_state() is True or (axes is not None and "door" in axes):
            await self._io.home_door()
        else:
            await self._io.ensure_door_homed()
        # A grip fault inside the fridge leaves its door open; the tool is out now, so close it.
        fridge = getattr(self, "_fridge", None)
        if fridge is not None and axes is not None and "fridge_door" in axes:
            await fridge.home_door()
        elif fridge is not None and fridge._door.is_homed() is True:
            await fridge.close_door()
        tool_axes = [axis for axis in axes or [] if axis not in ("door", "fridge_door")]
        if tool_axes:
            await self._tool.home(axes=tool_axes)

    async def read_tray_held(self) -> bool:
        return await self._tool.read_tray_held()

    async def grip(self) -> None:
        await self._tool._gs.open()

    # ------------------------------------------------------------------ #
    #  Parameters
    # ------------------------------------------------------------------ #

    def reload_parameters(self) -> None:
        self._tool.reload_parameters()
        self._io.reload_parameters()
