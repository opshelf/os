import asyncio
from typing import Awaitable, Callable, Optional

from hardware.controller import SharedModbusController as Controller
from hardware.exceptions import MotorError as HardwareMotorError
from hardware.motor import Motor
from hardware.curtain import CurtainSensor, CurtainMonitor
from openshelf_os.exceptions import (
    IOBoxError, MotorError, ConfigError
)
import openshelf_os.common as c
from openshelf_os.common import load_config_file

from hardware.scale import Scale
from enum import Enum
import logging
import sentry_sdk
import time


DOOR_LIMIT_ALARM_CODE = 32
DOOR_HOME_VERIFY_TOLERANCE_MM = 1.0
DOOR_ZERO_POSITION_TOLERANCE_MM = 0.01


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
        cfg = motors.by_name(name)
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


class IOBoxStatus(Enum):
    IDLE = 0
    READY = 1
    ERROR = 2


class IOBox:
    def __init__(self,
                 motorcontroller: Controller,
                 scalecontroller: Controller,
                 params: c.IOBlock,
                 safety=None,
                 gpio=None,
                 ):

        self._motorcontroller = motorcontroller
        self._status = IOBoxStatus.IDLE
        self._params = params
        self._logger = logging.getLogger(__name__).getChild("IOBox")
        self._gpio = gpio
        self._init_reporter: Optional[Callable[[str, str, str, Optional[str]], None]] = None

        self._md: Optional[Motor] = _create_motor_if_enabled(
            motorcontroller, self._params.motors, "door", safety, self._logger, self._gpio,
            home_limit_pin="io_lim_min",
        )
        scale_cfg = getattr(self._params, "scale", None)
        self._scale_enabled = scale_cfg is None or getattr(scale_cfg, "enabled", True)
        self._scale = Scale(scalecontroller) if self._scale_enabled is True else None
        self._scale_bus_ready = True
        self._scale_ready = False
        self._curtain_monitors: list[CurtainMonitor] = []
        self._curtain_state: Optional[bool] = None
        self._curtain_listener = None
        self._door_open: Optional[bool] = None
        self._door_last_read_at: Optional[float] = None
        self._door_min_refresh_s = 0.2
        curtain_cfg = getattr(self._params, "curtain", None)
        self._door_open_threshold_mm: float = getattr(
            curtain_cfg, "door_open_threshold_mm", 5.0
        )
        self._door_debounce_s: float = getattr(curtain_cfg, "door_debounce_s", 0.0)
        self._door_candidate_state: Optional[bool] = None
        self._door_candidate_since: Optional[float] = None
        self._door_force_refresh_on_block: bool = False
        self._safety_guard_bypassed: bool = False
        self._light_curtain_bypassed: bool = False
        self._door_bad_reading_count: int = 0
        self._door_max_bad_readings: int = 5
        self._door_max_position_mm: float = 1000.0
        self._door_home_verify_tolerance_mm: float = DOOR_HOME_VERIFY_TOLERANCE_MM
        self._door_zero_position_tolerance_mm: float = DOOR_ZERO_POSITION_TOLERANCE_MM
        self._door_motion_lock = asyncio.Lock()
        # Set when the end-of-cycle door home fails, so the next cycle homes before it moves an item.
        self._door_home_pending = False

    def reload_parameters(self) -> None:
        config = load_config_file(c.CONFIG_PATH)
        io_config = config.get("robot.io", None)

        if io_config is None:
            raise ConfigError("robot.io not found in config")

        self._params = io_config

        if self._md is not None:
            try:
                self._md._params = self._params.motors.by_name("door")
            except KeyError:
                self._logger.warning("Door motor missing while reloading IO parameters")

        self._logger.info("IO parameters reloaded from config.json")

    def _emit_init(self, component: str, phase: str, detail: str = "", reason: Optional[str] = None) -> None:
        reporter = getattr(self, "_init_reporter", None)
        if reporter is None:
            return
        try:
            reporter(component, phase, detail, reason)
        except Exception:
            pass

    async def _safe_post_configure(self, home_on_init: bool = True) -> None:
        steps: list[tuple[str, Callable[[], Awaitable[object]]]] = []
        if home_on_init is True:
            steps.append(("home_door", self.home_door))
        steps.extend(
            (
                ("refresh_door_state", self.refresh_door_state),
                ("configure_curtain", self._configure_curtain),
            )
        )
        for label, step in steps:
            try:
                await step()
            except Exception as e:
                self._logger.warning("init iobox %s failed: %s", label, e)

    async def configure(self, isolate: bool = False, scale_bus_ready: bool = True, home_on_init: bool = True):
        self._scale_bus_ready = scale_bus_ready

        if self._md is not None:
            try:
                await self._md.configure()
                if home_on_init is False:
                    self._emit_init("door", "ready")
            except Exception as e:
                self._status = IOBoxStatus.ERROR
                if isolate is False:
                    raise IOBoxError(f"IOBox configuration failed: {str(e)}") from e
                self._logger.warning("init door config failed: %s", e)
        else:
            self._logger.info("Door motor disabled via config")

        if self._scale_enabled is True:
            if self._scale_bus_ready is True:
                self._scale_ready = True
                self._emit_init("scale", "ready")
            else:
                self._scale_ready = False
                self._emit_init("scale", "failed", reason="scale bus unavailable")
        else:
            self._logger.info("Scale disabled via config")

        if self._status is not IOBoxStatus.ERROR:
            self._status = IOBoxStatus.IDLE

        if isolate is True:
            await self._safe_post_configure(home_on_init=home_on_init)
        else:
            if home_on_init is True:
                await self.home_door()
            await self.refresh_door_state()
            await self._configure_curtain()

    async def _configure_curtain(self) -> None:
        curtain_cfg = getattr(self._params, "curtain", None)

        if curtain_cfg is None:
            self._logger.info("Curtain sensor configuration missing; skipping setup")
            return

        if not curtain_cfg.enabled:
            self._logger.info("Curtain sensor disabled via config")
            return

        self._emit_init("light_curtain", "configuring")
        try:
            poll_interval = curtain_cfg.poll_interval_s

            if self._gpio is None or self._gpio.has_pin("light_curtain") is False:
                raise ConfigError("Light curtain requires shared GPIO pin 'light_curtain'")

            curtain_input = self._gpio.input("light_curtain")
            curtain_sensor = CurtainSensor(input_device=curtain_input)
            curtain_monitor = CurtainMonitor(
                curtain_sensor,
                poll_interval=poll_interval,
                on_change=self._handle_curtain_change,
            )
            self._curtain_monitors = [curtain_monitor]
            await curtain_monitor.start()

            self._logger.info(
                "Curtain monitor started",
                extra={
                    "pin_name": "light_curtain",
                    "pin": curtain_input.pin,
                    "board": getattr(curtain_input, "board", None),
                    "poll_interval_s": poll_interval,
                    "shared_gpio": True,
                },
            )
            self._emit_init("light_curtain", "ready")
        except Exception as exc:
            self._emit_init("light_curtain", "failed", reason=str(exc))
            raise

    async def _ensure_scale_ready(self) -> None:
        if self._scale_enabled is False:
            return

        if self._scale_ready is True:
            return

        if self._scale_bus_ready is False:
            raise IOBoxError("Scale bus is not available")

        self._scale_ready = True

    async def get_weight(self) -> Optional[float]:
        if self._scale_enabled is False or self._scale is None:
            return None

        start_time = time.monotonic()

        try:
            await self._ensure_scale_ready()
            weight = await self._scale.wait_for_stable_weight(timeout=15.0, poll_interval=0.1)
            elapsed = time.monotonic() - start_time

            if weight is None:
                self._logger.info(
                    "Scale returned no measurement",
                    extra={"elapsed_s": round(elapsed, 3), "timeout_s": 15.0},
                )
            else:
                self._logger.info(
                    "Scale stabilized",
                    extra={"elapsed_s": round(elapsed, 3), "weight_grams": weight},
                )

            return weight
        except Exception as e:
            elapsed = time.monotonic() - start_time
            self._logger.error(
                "Scale read failed after %.3fs: %s", elapsed, e, extra={"elapsed_s": round(elapsed, 3)}
            )
            sentry_sdk.capture_exception(e)
            return None

    def set_safety_guard_bypassed(self, enabled: bool) -> None:
        self._safety_guard_bypassed = enabled

    def set_light_curtain_bypassed(self, enabled: bool) -> None:
        self._light_curtain_bypassed = enabled

    def _is_door_motion_safety_explicitly_clear(self) -> bool:
        if self._curtain_state is not False or self._md is None:
            return False

        safety = getattr(self._md, "_safety", None)
        if safety is None:
            return False

        try:
            return safety.blocked is False
        except Exception:
            return False

    async def _is_recoverable_door_move_fault(self, exc: HardwareMotorError) -> bool:
        if self._is_door_motion_safety_explicitly_clear() is False:
            return False

        fault_context = getattr(exc, "fault_context", None)

        if not isinstance(fault_context, dict):
            return False

        if not all(
            fault_context.get(field) is False
            for field in ("stall_detected", "no_progress", "safety_blocked")
        ):
            return False

        if "alarm_code" not in fault_context:
            return False

        alarm_code = fault_context["alarm_code"]
        if alarm_code is not None:
            return alarm_code == DOOR_LIMIT_ALARM_CODE

        if self._md is None:
            return False

        try:
            alarm_code = await self._md.get_param("Current alarm")
        except Exception as alarm_exc:
            self._logger.warning(
                "Could not classify door movement fault for retry: %s",
                alarm_exc,
            )
            return False

        return (
            alarm_code == DOOR_LIMIT_ALARM_CODE
            and self._is_door_motion_safety_explicitly_clear()
        )

    async def _verify_door_homed(self) -> None:
        if self._md is None:
            raise IOBoxError("Door motor not configured or disabled")

        position_mm = await self._md.get_position()
        tolerance_mm = self._door_home_verify_tolerance_mm

        if position_mm is None or abs(position_mm) > tolerance_mm:
            raise HardwareMotorError(
                "Door recovery homing did not establish a valid zero position "
                f"(position={position_mm}, tolerance={tolerance_mm})"
            )

    async def _move_door_with_retry(
        self,
        target_mm: float,
        *,
        operation: str,
        rpm: Optional[int] = None,
    ) -> None:
        if self._md is None:
            raise IOBoxError("Door motor not configured or disabled")

        try:
            await self._md.move_to_position(target_mm, rpm=rpm)
            return
        except HardwareMotorError as exc:
            if await self._is_recoverable_door_move_fault(exc) is False:
                raise

            self._logger.warning(
                "Recoverable door limit alarm; homing before one movement retry",
                extra={
                    "operation": operation,
                    "target_mm": target_mm,
                    "alarm_code": DOOR_LIMIT_ALARM_CODE,
                },
            )

            if self._is_door_motion_safety_explicitly_clear() is False:
                raise exc

            await self._home_door_and_cache_closed()
            await self._verify_door_homed()

            self._logger.info(
                "Retrying door movement after successful recovery home",
                extra={"operation": operation, "target_mm": target_mm},
            )
            await self._md.move_to_position(target_mm, rpm=rpm)

    async def open_door(self):
        if self._md is None:
            raise IOBoxError("Door motor not configured or disabled")

        try:
            async with self._door_motion_lock:
                while (
                    self._curtain_state
                    and self._safety_guard_bypassed is False
                    and self._light_curtain_bypassed is False
                ):
                    await asyncio.sleep(0.05)

                await self._move_door_with_retry(
                    self._params.motors.by_name("door").lim,
                    operation="open",
                )
                await self.refresh_door_state(force=True)
        except Exception as e:
            self._status = IOBoxStatus.ERROR

            if isinstance(e, IOBoxError):
                raise

            raise MotorError(f"Failed to open door: {str(e)}") from e

    async def close_door(self):
        if self._md is None:
            raise IOBoxError("Door motor not configured or disabled")

        try:
            async with self._door_motion_lock:
                door_cfg = self._params.motors.by_name("door")
                await self._move_door_with_retry(
                    door_cfg.lower_lim,
                    operation="close",
                    rpm=door_cfg.ret_rpm,
                )
                await self.refresh_door_state(force=True)
        except Exception as e:
            self._status = IOBoxStatus.ERROR

            if isinstance(e, IOBoxError):
                raise

            raise MotorError(f"Failed to close door: {str(e)}") from e

    async def home_door(self) -> None:
        if self._md is None:
            self._logger.info("Door motor not configured; skipping home")
            return

        # Pending until the home completes, so a home cancelled mid-way is redone next cycle.
        self._door_home_pending = True
        try:
            async with self._door_motion_lock:
                if not self._curtain_state:
                    door_cfg = self._params.motors.by_name("door")
                    await self._move_door_with_retry(
                        door_cfg.lower_lim,
                        operation="pre_home_close",
                        rpm=door_cfg.ret_rpm,
                    )
                await self._home_door_and_cache_closed()
                await self.refresh_door_state(force=True)
                # The forced refresh is diagnostic only: a completed home is
                # authoritative even if the immediately following read is stale.
                self._cache_door_closed_after_home()
                self._door_home_pending = False

        except Exception as e:
            self._status = IOBoxStatus.ERROR

            if isinstance(e, IOBoxError):
                raise

            raise MotorError(f"Failed to home door: {str(e)}") from e

    async def home_door_after_cycle(self) -> bool:
        """Re-home the closed door at the end of an IO cycle without failing the cycle.

        The item is already on its shelf by then, so failing the command would leave the
        inventory saying it never arrived. A failed home is deferred to the next cycle.
        """
        try:
            await self.home_door()
            return True
        except Exception as exc:
            self._logger.warning(
                "Door home after IO cycle failed; homing before the next cycle",
                extra={"error": str(exc)},
            )
            return False

    async def ensure_door_homed(self) -> None:
        """Finish a deferred door home before an IO cycle moves any item."""
        if self._door_home_pending is True:
            await self.home_door()

    def _cache_door_closed_after_home(self) -> None:
        """Record the known physical door position after a successful home."""
        self._door_open = False
        self._door_candidate_state = None
        self._door_candidate_since = None
        self._door_bad_reading_count = 0

    async def _home_door_and_cache_closed(self) -> None:
        """Home the door and record its authoritative closed state on success."""
        if self._md is None:
            raise IOBoxError("Door motor not configured or disabled")

        await self._md.home()
        self._cache_door_closed_after_home()


    def _handle_curtain_change(self, state: Optional[bool]) -> None:
        previous = self._curtain_state
        self._curtain_state = state

        if state:
            self._door_force_refresh_on_block = True

        self._logger.debug(
            "Curtain state change",
            extra={
                "blocked": state,
                "previous": previous,
                "force_door_refresh": bool(state),
            },
        )

        if self._curtain_listener:
            try:
                self._curtain_listener(state)
            except Exception as exc:
                self._logger.warning("Curtain listener failed: %s", exc)

    def get_curtain_state(self) -> Optional[bool]:
        return self._curtain_state

    def get_door_state(self) -> Optional[bool]:
        return self._door_open

    def _door_zero_clamp_mm(self) -> float:
        """How far below zero a closed door may read.

        A torque-homed door springs back off its stop (lb2-3 reads -0.44 mm), and the door's
        home_zero_tolerance_mm already accepts that as a good home; counting it as a bad
        reading re-homed the door every few seconds, onto the same reading.
        """
        home_tolerance_mm = 0.0
        try:
            door_cfg = self._params.motors.by_name("door")
            home_tolerance_mm = float(getattr(door_cfg, "home_zero_tolerance_mm", 0.0) or 0.0)
        except Exception:
            pass
        return max(self._door_zero_position_tolerance_mm, home_tolerance_mm)

    def _normalize_door_position(self, position_mm: float) -> float:
        tolerance_mm = self._door_zero_clamp_mm()
        if -tolerance_mm <= position_mm < 0:
            self._logger.debug(
                "Clamping near-zero door position",
                extra={
                    "position_mm": position_mm,
                    "tolerance_mm": tolerance_mm,
                },
            )
            return 0.0

        return position_mm

    def _is_door_position_out_of_range(self, position_mm: float, door_limit: float) -> bool:
        return (
            position_mm < 0
            or position_mm > door_limit * 1.5
            or position_mm > self._door_max_position_mm
        )

    async def refresh_door_state(self, *, force: bool = False) -> Optional[bool]:
        if self._md is None:
            return self._door_open

        # The door only sits in a busy window while stationary (0x1122 is written before the
        # home trigger and after the zero), so the cache is current. Reading would sleep out
        # the window under the worker's safety-guard lock, forced or not; a pending forced
        # refresh stays pending.
        if self._md.is_bus_busy():
            return self._door_open

        if self._door_force_refresh_on_block:
            self._logger.debug("Forcing door refresh after curtain block")
            force = True
            self._door_force_refresh_on_block = False

        now = time.monotonic()

        if not force and self._door_last_read_at is not None:
            if now - self._door_last_read_at < self._door_min_refresh_s:
                return self._door_open

        try:
            position_mm = await self._md.get_position()
        except Exception as exc:
            self._door_last_read_at = now
            self._logger.debug("Door position read failed: %s", exc)

            return self._door_open

        if position_mm is None:
            self._door_last_read_at = now
            self._logger.debug(
                "Door position read returned None; using cached state",
                extra={"cached_open": self._door_open},
            )

            return self._door_open

        door_limit = self._params.motors.by_name("door").lim

        while True:
            position_mm = self._normalize_door_position(position_mm)

            if self._is_door_position_out_of_range(position_mm, door_limit) is False:
                break

            self._door_last_read_at = now
            self._door_bad_reading_count += 1

            self._logger.warning(
                "Door position out of range; using cached state",
                extra={
                    "position_mm": position_mm,
                    "door_limit": door_limit,
                    "cached_open": self._door_open,
                    "bad_reading_count": self._door_bad_reading_count,
                },
            )

            if self._door_bad_reading_count >= self._door_max_bad_readings:
                if self._door_motion_lock.locked():
                    self._logger.warning(
                        "Deferring bad-reading door recovery while a door motion is active",
                        extra={"bad_reading_count": self._door_bad_reading_count},
                    )
                else:
                    self._logger.warning(
                        "Too many bad door readings; attempting door motor recovery",
                        extra={"bad_reading_count": self._door_bad_reading_count},
                    )

                    await self._door_motion_lock.acquire()
                    try:
                        recovery_position_mm = await self._md.get_position()
                        recovery_position_valid = recovery_position_mm is not None

                        if recovery_position_valid:
                            position_mm = recovery_position_mm
                            position_mm = self._normalize_door_position(position_mm)
                            recovery_position_valid = not self._is_door_position_out_of_range(
                                position_mm,
                                door_limit,
                            )

                        if recovery_position_valid:
                            self._door_bad_reading_count = 0
                            self._logger.info(
                                "Door position recovered before homing; skipping recovery home",
                                extra={"position_mm": position_mm},
                            )
                            break
                        else:
                            await self._home_door_and_cache_closed()
                            self._logger.info("Door motor homed successfully after bad readings")
                    except Exception as home_exc:
                        self._logger.error("Door motor recovery failed: %s", home_exc)
                    finally:
                        self._door_motion_lock.release()

            return self._door_open

        self._door_bad_reading_count = 0
        threshold = self._door_open_threshold_mm
        inferred = position_mm > threshold

        if inferred == self._door_open:
            self._door_candidate_state = None
            self._door_candidate_since = None
            self._door_last_read_at = now

            return self._door_open

        if self._door_debounce_s <= 0:
            self._door_open = inferred
            self._door_last_read_at = now
            self._logger.debug(
                "Door state inferred",
                extra={"position_mm": position_mm, "open": inferred, "threshold_mm": threshold},
            )

            return self._door_open

        if self._door_candidate_state != inferred:
            self._door_candidate_state = inferred
            self._door_candidate_since = now
            return self._door_open

        assert self._door_candidate_since is not None

        if now - self._door_candidate_since >= self._door_debounce_s:
            self._door_open = inferred
            self._door_last_read_at = now
            self._door_candidate_state = None
            self._door_candidate_since = None
            self._logger.debug(
                "Door state inferred",
                extra={
                    "position_mm": position_mm,
                    "open": inferred,
                    "threshold_mm": threshold,
                    "debounce_s": self._door_debounce_s,
                },
            )

        return self._door_open

    def set_curtain_listener(self, listener) -> None:
        self._curtain_listener = listener

        if self._curtain_state is not None and self._curtain_listener:
            try:
                self._curtain_listener(self._curtain_state)
            except Exception as exc:
                self._logger.warning("Curtain listener failed: %s", exc)


    async def stop(self) -> None:
        errors = []

        if self._md is not None:
            try:
                await self._md.stop()
            except Exception as e:
                errors.append(f"Door motor stop failed: {str(e)}")

        for curtain_monitor in list(self._curtain_monitors):
            try:
                await curtain_monitor.stop()
            except Exception as e:
                errors.append(f"Curtain monitor stop failed: {str(e)}")

        self._curtain_monitors = []

        if errors:
            self._status = IOBoxStatus.ERROR
            raise IOBoxError("Stop errors: " + "; ".join(errors))
