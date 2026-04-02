from hardware import motor_helpers
from hardware.controller import SharedModbusController
from hardware.home_sensor import HomeSensor
import time
import asyncio
import functools
import logging
from enum import Enum
from typing import Any, Optional, Callable, Awaitable

from hardware.exceptions import (
    MotorError, MotorTimeoutError, MotorLimitError,
    MotorConfigError
)

MOTOR_MOVE_TIMEOUT = 180.0
MOTOR_HOME_TIMEOUT = 180.0
MOTOR_JOG_TIMEOUT = 10.0
MOTOR_WAIT_TIMEOUT = 60.0
POSITION_TOLERANCE_MM = 1.0
DEFAULT_BRAKE_OUTPUT_FUNCTION = 0xA1
RELEASED_BRAKE_OUTPUT_FUNCTION = 0x24

logger = logging.getLogger(__name__)


class CurtainSafety:
    def __init__(self) -> None:
        self._clear = asyncio.Event()
        self._clear.set()
        self.blocked = False

    def block(self) -> None:
        self.blocked = True
        self._clear.clear()

    def unblock(self) -> None:
        self.blocked = False
        self._clear.set()

    async def wait_clear(self) -> None:
        await self._clear.wait()

class MotorStatus(Enum):
    IDLE = 0
    MOVING = 1
    STOPPED = 2
    ERROR = 3
    FAULT = 4
    TIMEOUT = 5

class Servo:
    """Controls a servo motor via Modbus."""

    def __init__(self, controller: SharedModbusController, params: dict | None = None):
        """Initialize servo controller.

        Args:
            controller: Shared Modbus controller instance
            params: Optional dict with motor_id, control_reg, status_reg
        """
        if params is None:
            params = {}
        self._motor_id = int(params.get("motor_id", 0x9))
        self._controller = controller
        self._control_reg = int(params.get("control_reg", 0x3E8))
        self._status_reg = int(params.get("status_reg", 0x07D0))

    async def configure(self) -> None:
        """Configure the servo with initial settings.
        
        Raises:
            MotorError: If configuration fails
            MotorTimeoutError: If activation times out
        """
        try:
            # Activate
            await self._controller.write_single_register(
                self._motor_id, 
                self._control_reg, 
                0x0  # clear
            )
            await self._controller.write_single_register(
                self._motor_id, 
                self._control_reg, 
                0x1  # enable
            )
            await self.wait_for_activate()

            # Static position settings
            # Open
            await self._controller.write_single_register(
                self._motor_id, 
                0x03EB, 
                0x8020  # 0x80|0x20
            )
            # Close
            await self._controller.write_single_register(
                self._motor_id, 
                0x03ED, 
                0x80FE  # 0x80|0xFE
            )
            # Safe open
            await self._controller.write_single_register(
                self._motor_id, 
                0x03EF, 
                0x80C0  # 0x80|0xFF
            )

            # Set configuration values
            await self._controller.write_single_register(self._motor_id, 0x03EC, 0x80)
            await self._controller.write_single_register(self._motor_id, 0x03EE, 0x80)
            await self._controller.write_single_register(self._motor_id, 0x03F0, 0x80)
            
        except Exception as e:
            raise MotorError(f"Failed to configure servo: {str(e)}") from e

    async def wait_for_activate(self):
        """Wait for motor to activate.
        
        Raises:
            MotorTimeoutError: If activation takes too long
            MotorError: If motor status read fails
        """
        start_time = time.monotonic()

        while True:
            status = await self._controller.read_holding_registers(self._motor_id, self._status_reg)
            if status is None:
                raise MotorError("Failed to read motor status during activation")

            activation_status = status & (1<<5)
            if activation_status:
                return

            if time.monotonic() - start_time > 3:
                raise MotorTimeoutError("Motor activation timed out after 3 seconds")

            await asyncio.sleep(0.01)

    async def wait_for_servo(self, timeout: float = 5.0) -> MotorStatus:
        """Wait for servo to complete its motion.
        
        Args:
            timeout: Maximum time to wait in seconds
            
        Raises:
            MotorTimeoutError: If servo motion takes longer than timeout
            MotorError: If motor status read fails
        """
        start_time = time.monotonic()

        while True:
            status = await self._controller.read_holding_registers(self._motor_id, self._status_reg)
            if status is None:
                raise MotorError("Failed to read motor status during servo motion")
                
            move_status = (status & (1<<6)) or (status & (1<<7))
            if move_status:
                return MotorStatus.STOPPED
                
            if time.monotonic() - start_time > timeout:
                raise MotorTimeoutError(f"Servo motion timed out after {timeout} seconds")
                
            await asyncio.sleep(0.01)

    async def open(self):
        """Open the servo to maximum position.
        
        Raises:
            MotorError: If servo control fails
            MotorTimeoutError: If operation times out
        """
        try:
            await self._controller.write_single_register(self._motor_id, self._control_reg, 0x080B)
            await self.wait_for_servo()
        except Exception as e:
            raise MotorError(f"Failed to open servo: {str(e)}") from e

    async def safe_open(self):
        """Open the servo to a safe intermediate position.
        
        Raises:
            MotorError: If servo control fails
            MotorTimeoutError: If operation times out
        """
        try:
            await self._controller.write_single_register(self._motor_id, self._control_reg, 0x0A0B)
            await self.wait_for_servo()
        except Exception as e:
            raise MotorError(f"Failed to safe-open servo: {str(e)}") from e

    async def close(self):
        await self._controller.write_single_register(self._motor_id, self._control_reg, 0x090B) 
        await self.wait_for_servo()
         
# stepper
class Motor():
    _motor_id: int
    _controller: SharedModbusController
    _params: Any  # Consider creating a MotorParams type
    _cur_pos: Optional[int]
    _stall_timeout: float
    _status: MotorStatus

    def __init__(self, controller: SharedModbusController, params: Any, safety: Optional[CurtainSafety] = None) -> None:
        self._motor_id = params.motor_id
        self._controller = controller
        self._params = params
        self._cur_pos = None
        self._stall_timeout = 2.0
        self._status = MotorStatus.IDLE
        self._pending_motion: Optional[Callable[[], Awaitable[None]]] = None
        self._target_pos: Optional[float] = None
        self._logger = logger.getChild(f"motor.{self._motor_id}")
        self._safety = safety
        self._simulated_fault = False
        self._on_move_complete: Optional[Callable[[float], None]] = None
        self._home_sensor: Optional[HomeSensor] = None
        self._is_homed = False
        self._hard_stopped = False
        self._configured_brake_output_function = int(
            getattr(self._params, "brake_output_function", DEFAULT_BRAKE_OUTPUT_FUNCTION)
        )
        self._active_brake_output_function = self._configured_brake_output_function

    def is_homed(self) -> bool:
        return self._is_homed

    def is_hard_stopped(self) -> bool:
        return self._hard_stopped

    def cleanup(self) -> None:
        if self._home_sensor is None:
            return

        try:
            self._home_sensor.cleanup()
        finally:
            self._home_sensor = None

    async def set_brake_output_function(self, value: int) -> None:
        brake_output_function = int(value)

        if self._active_brake_output_function == brake_output_function:
            return

        await self.set_param("SO2 output function", brake_output_function)
        self._active_brake_output_function = brake_output_function

    async def disable_brake_output(self) -> None:
        await self.set_brake_output_function(RELEASED_BRAKE_OUTPUT_FUNCTION)

    async def restore_brake_output(self) -> None:
        await self.set_brake_output_function(self._configured_brake_output_function)

    def _get_home_sensor_config(self) -> Any:
        sensor_cfg = getattr(self._params, "home_sensor", None)
        if sensor_cfg is None:
            return None

        if getattr(sensor_cfg, "enabled", True) is False:
            return None

        return sensor_cfg

    def _get_home_sensor(self) -> Optional[HomeSensor]:
        sensor_cfg = self._get_home_sensor_config()
        if sensor_cfg is None:
            return None

        if self._home_sensor is None:
            self._home_sensor = HomeSensor(
                pin=sensor_cfg.pin,
                inverted=getattr(sensor_cfg, "inverted", True),
            )

        return self._home_sensor

    def _get_home_direction(self) -> int:
        p0 = self._params.phys_at_zero if self._params.phys_at_zero is not None else 0
        pm = self._params.phys_at_max if self._params.phys_at_max is not None else self._params.lim
        is_inverted = p0 > pm
        return int(self._params.dr) ^ 1 ^ (1 if is_inverted else 0)

    async def _prepare_home_motion(self, rpm: int, direction: int) -> None:
        await self.set_param("Control word", 0x1111)
        await self.set_param("Control word", 0x1122)
        await self._controller.write_single_register(self._motor_id, 0x6000, 0x0000)
        self._status = MotorStatus.STOPPED
        await self.set_param("Motion of Path 0", 0x2)
        await self.set_param("Motor direction", direction)
        await self.set_param("Velocity", rpm)

    async def _start_path_zero(self) -> None:
        await self._controller.write_single_register(
            self._motor_id,
            motor_helpers.TRIGGER,
            0x10,
        )
        self._status = MotorStatus.MOVING

    async def _set_zero_position(self) -> None:
        await self._controller.write_single_register(
            self._motor_id,
            motor_helpers.TRIGGER,
            0x21,
        )

    async def _wait_for_home_sensor(self, sensor: HomeSensor, timeout: float, poll_interval: float) -> None:
        start_time = time.monotonic()

        while True:
            if sensor.read() is True:
                await self.stop()
                return

            status_reg = getattr(self._params, "status_reg", 0x1003)
            status = await self._controller.read_holding_registers(self._motor_id, status_reg)
            if status is None:
                await self.stop()
                raise MotorError(f"Failed to read motion status from motor {self._motor_id}")

            fault_bit_num = getattr(self._params, "status_fault_bit", 0)
            fault_bit = (status & (1 << fault_bit_num)) if fault_bit_num is not None else 0

            try:
                alarm_code = await self.get_param("Current alarm")
            except Exception as exc:
                await self.stop()
                raise MotorError(f"Failed to read motor {self._motor_id} alarm during homing: {exc}") from exc

            if fault_bit or alarm_code not in (None, 0):
                await self.stop()
                self._is_homed = False
                self._hard_stopped = True
                raise MotorError(
                    f"Motor {self._motor_id} faulted before home sensor triggered"
                    f" (alarm={alarm_code})"
                )

            if time.monotonic() - start_time > timeout:
                await self.stop()
                raise MotorTimeoutError(f"Motor {self._motor_id} homing timed out waiting for sensor")

            await asyncio.sleep(poll_interval)

    async def _release_home_sensor(self, sensor: HomeSensor, sensor_cfg: Any) -> None:
        if sensor.read() is False:
            return

        release_mode = getattr(sensor_cfg, "release_mode", "jog")
        release_backoff_mm = sensor_cfg.release_backoff_mm

        for _ in range(10):
            if release_mode == "absolute":
                release_target_mm = getattr(sensor_cfg, "release_target_mm", None)

                if release_target_mm is None:
                    raise MotorError(f"Motor {self._motor_id} missing release target for absolute mode")

                target_position = min(release_target_mm, self._params.lim)

                await self.move_to_position(target_position)
            else:
                await self.jog(release_backoff_mm)

            if sensor.read() is False:
                return

        raise MotorError(f"Motor {self._motor_id} could not clear home sensor")

    async def _home_with_sensor(self, sensor_cfg: Any) -> None:
        sensor = self._get_home_sensor()
        if sensor is None:
            raise MotorError(f"Motor {self._motor_id} home sensor is not available")

        home_dir = self._get_home_direction()
        home_rpm = self._params.home_rpm or self._params.ret_rpm
        precision_rpm = sensor_cfg.precision_rpm or max(1, home_rpm // 4)
        should_disable_brake_during_home = getattr(sensor_cfg, "disable_brake_during_home", False)
        did_disable_brake = False

        try:
            if should_disable_brake_during_home is True:
                await self.disable_brake_output()
                did_disable_brake = True

            if sensor.read() is True:
                await self._set_zero_position()
                await self.move_config()

            await self._release_home_sensor(sensor, sensor_cfg)
            await self._prepare_home_motion(home_rpm, home_dir)
            await self._start_path_zero()
            await self._wait_for_home_sensor(sensor, MOTOR_HOME_TIMEOUT, sensor_cfg.poll_interval_s)

            await self._set_zero_position()
            await self.move_config()
            await self._release_home_sensor(sensor, sensor_cfg)
            await self._prepare_home_motion(precision_rpm, home_dir)
            await self._start_path_zero()
            await self._wait_for_home_sensor(sensor, MOTOR_HOME_TIMEOUT, sensor_cfg.poll_interval_s)

            await self._set_zero_position()
            await self.move_config()
            self._is_homed = True
            self._hard_stopped = False
        finally:
            if did_disable_brake is True:
                try:
                    await self.restore_brake_output()
                except Exception as exc:
                    self._logger.warning(
                        "Failed to restore brake output after homing",
                        extra={"motor_id": self._motor_id, "error": str(exc)},
                    )

    async def configure(self):
        """Configure the stepper motor with initial parameters.
        
        Raises:
            MotorConfigError: If any configuration parameter fails to set
            MotorError: If communication with motor fails
        """
        try:
            self._logger.info(
                "Configuring motor",
                extra={"motor_id": self._motor_id, "limit_mm": self._params.lim},
            )
            # Clear any existing alarms
            await self.set_param("Control word", 0x1111)
            await self.set_param("Control word", 0x1122)

            # Disable auto-homing to prevent blocking motion commands
            await self._controller.write_single_register(self._motor_id, 0x6000, 0x0000)

            # Configure motor parameters
            await self.set_param("SO2 output function", self._configured_brake_output_function)
            self._active_brake_output_function = self._configured_brake_output_function
            await self._controller.write_single_register(self._motor_id, 0x0167, 100)
            await self._controller.write_single_register(self._motor_id, 0x0169, 100)
            await self._controller.write_single_register(self._motor_id, 0x016B, 10)
            max_following_error = getattr(self._params, "max_following_error", 4000)
            await self.set_param("Allowed max position following error", max_following_error)
            await self.set_param("Position loop Kp", 50)
            await self.set_param("Max stop time", 100)
            await self.set_param("Acceleration", self._params.accel)
            await self.set_param("Deceleration", self._params.accel)
            await self.set_param("Peak current", self._params.Ip)
            
            # Configure movement parameters
            await self.move_config()
        except Exception as e:
            self._logger.error(
                "Failed to configure motor",
                extra={"motor_id": self._motor_id, "error": str(e)},
                exc_info=True,
            )
            raise MotorConfigError(f"Failed to configure motor {self._motor_id}: {str(e)}") from e

    async def _read_status(self) -> MotorStatus:
        """Read the current motor status word."""
        try:
            status_val = await self._controller.read_holding_registers(
                self._motor_id,
                0x0000
            )
            if status_val is None:
                return MotorStatus.ERROR
            try:
                return MotorStatus(int(status_val))
            except Exception:
                return MotorStatus.ERROR
        except Exception:
            return MotorStatus.ERROR

    # TODO add axis limits
    async def move_to_position(self, value: float, rpm: Optional[int] = None, accel: Optional[int] = None) -> None:  # mm
        """Move the motor to an absolute position in millimeters.
        
        Args:
            value: Target position in millimeters
            rpm: Optional velocity override in RPM
            accel: Optional acceleration/deceleration override in ms/1000rpm
            
        Raises:
            MotorLimitError: If target position exceeds motor limits
            MotorError: If movement fails or times out
        """
        try:
            if self._safety:
                if self._safety.blocked:
                    self._logger.debug(
                        "Curtain guard active before move; waiting to clear",
                        extra={"motor_id": self._motor_id, "target_mm": value},
                    )
                await self._safety.wait_clear()

            t0 = time.monotonic()
            await self._start_absolute_move(value, rpm, accel)
            status = await self.wait_for_move(timeout=MOTOR_MOVE_TIMEOUT)
            elapsed = time.monotonic() - t0

            if self._on_move_complete is not None:
                self._on_move_complete(elapsed)

            if status != MotorStatus.STOPPED:
                raise MotorError(
                    f"Movement failed for motor {self._motor_id} (status={status.name})"
                )

            self._hard_stopped = False

        except Exception as e:
            if not isinstance(e, MotorError):
                raise MotorError(f"Failed to move motor {self._motor_id} to position {value}mm: {str(e)}") from e
            raise
    
    async def move_to_position_no_wait(self, value: float, rpm: Optional[int] = None, accel: Optional[int] = None) -> None:
        """Move motor to position without waiting for completion.
        
        Args:
            value: Target position in millimeters
            rpm: Optional velocity override in RPM
            accel: Optional acceleration/deceleration override in ms/1000rpm
            
        Raises:
            MotorLimitError: If target position exceeds motor limits
            MotorError: If movement setup fails
        """
        try:
            if self._safety:
                if self._safety.blocked:
                    self._logger.debug(
                        "Curtain guard active before move; waiting to clear",
                        extra={"motor_id": self._motor_id, "target_mm": value},
                    )
                await self._safety.wait_clear()
            await self._start_absolute_move(value, rpm, accel)
            
        except Exception as e:
            if not isinstance(e, (MotorError, MotorLimitError)):
                raise MotorError(f"Failed to start movement to {value}mm: {str(e)}") from e
            raise

    async def _start_absolute_move(self, value: float, rpm: Optional[int] = None, accel: Optional[int] = None) -> None:
        """Setup and trigger an absolute move, tracking it for retries."""
        original_value = value

        lower_lim = getattr(self._params, "lower_lim", 0)

        if value < lower_lim:
            value = lower_lim
        if value > self._params.lim:
            value = self._params.lim

        if value != original_value:
            self._logger.warning(
                "Target position clamped to motor limits",
                extra={
                    "motor_id": self._motor_id,
                    "requested_mm": original_value,
                    "clamped_mm": value,
                    "limit_mm": self._params.lim,
                },
            )

        self._target_pos = value
        physical_pos = self._params.logical_to_physical(value)

        ppr = getattr(self._params, "ppr", 10000)
        pulse = motor_helpers.rev_to_pulse(motor_helpers.mm_to_rev(physical_pos, self._params.mpr), ppr)

        if rpm is not None:
            await self.set_param("Velocity", int(rpm))
        if accel is not None:
            await self.set_param("Acceleration", int(accel))
            await self.set_param("Deceleration", int(accel))

        # Set target position
        await self.set_param("Position High", pulse[0])
        await self.set_param("Position Low", pulse[1])
        self._logger.debug(
            "Set position registers",
            extra={
                "motor_id": self._motor_id,
                "target_logical_mm": value,
                "physical_mm": physical_pos,
                "pulse_high": pulse[0],
                "pulse_low": pulse[1],
                "total_pulses": (pulse[0] << 16) | pulse[1],
            }
        )

        # Start movement
        await self._controller.write_single_register(
            self._motor_id,
            motor_helpers.TRIGGER,
            0x10  # start path 0
        )

        # Small delay to allow motor driver to clear complete bit
        await asyncio.sleep(0.1)

        self._status = MotorStatus.MOVING
        self._pending_motion = functools.partial(self._start_absolute_move, value, rpm, accel)
        self._logger.debug(
            "Started absolute move",
            extra={
                "motor_id": self._motor_id,
                "target_mm": value,
                "limit_mm": self._params.lim,
                "rpm": rpm or self._params.rpm,
                "accel": accel or self._params.accel,
            }
        )
    
    async def move_config(self) -> None:
        """Configure motor movement parameters.

        Raises:
            MotorConfigError: If configuration fails
        """
        try:
            # Clear any alarms
            await self.set_param("Control word", 0x1111)
            await self.set_param("Control word", 0x1122)

            # Disable auto-homing after control word writes (which may reset it)
            await self._controller.write_single_register(self._motor_id, 0x6000, 0x0000)

            # Configure movement parameters
            await self.set_param("Motion of Path 0", 0x1)  # abs pos 0x1
            await self.set_param("Motor direction", self._params.dr)
            await self.set_param("Velocity", self._params.rpm)

        except Exception as e:
            raise MotorConfigError(f"Failed to configure movement parameters: {str(e)}") from e

    async def clear_alarm(self) -> None:
        """Clear motor alarms without homing.

        Sends control word sequence to reset fault state and reconfigure for movement.
        Verifies the alarm actually cleared by reading the alarm register.

        Raises:
            MotorError: If alarm clear fails or alarm persists after clear
        """
        try:
            await self.set_param("Control word", 0x1111)
            await self.set_param("Control word", 0x1122)
            await self._controller.write_single_register(self._motor_id, 0x6000, 0x0000)
            await self.move_config()

            alarm_code: Optional[int] = None
            for _ in range(3):
                alarm_code = await self.get_param("Current alarm")
                if alarm_code is None or alarm_code in (0, 32):
                    break
                await asyncio.sleep(0.05)
            if alarm_code is not None and alarm_code not in (0, 32):
                raise MotorError(f"Alarm {alarm_code} persists after clear attempt")

            self._status = MotorStatus.STOPPED
        except Exception as e:
            if not isinstance(e, MotorError):
                raise MotorError(f"Failed to clear motor alarm: {str(e)}") from e
            raise

    async def get_position(self) -> float:
        """Get the current motor position in millimeters.

        Raises:
            MotorError: If position read fails or calculation fails

        Returns:
            Current position in millimeters
        """
        try:
            reg_high = getattr(self._params, "position_reg_high", 0x602C)
            reg_low = getattr(self._params, "position_reg_low", 0x602D)

            pos_l = await self._controller.read_holding_registers(self._motor_id, reg_low)
            pos_h = await self._controller.read_holding_registers(self._motor_id, reg_high)
            
            if pos_l is None or pos_h is None:
                raise MotorError(f"Failed to read position registers for motor {self._motor_id}")
            
            # Combine high and low position values
            pos = (pos_h << 16) | pos_l
            if not pos:
                return 0.0
                
            ppr = getattr(self._params, "ppr", 10000)

            physical_pos = (pos / ppr) * self._params.mpr
            logical_pos = self._params.physical_to_logical(physical_pos)

            max_valid = self._params.lim * 2
            if abs(logical_pos) > max_valid:
                self._logger.warning(
                    "Garbage position detected, retrying",
                    extra={
                        "motor_id": self._motor_id,
                        "raw_pos": pos,
                        "logical_pos": logical_pos,
                        "limit": self._params.lim,
                    },
                )
                await asyncio.sleep(0.1)
                pos_l = await self._controller.read_holding_registers(self._motor_id, reg_low)
                pos_h = await self._controller.read_holding_registers(self._motor_id, reg_high)
                if pos_l is None or pos_h is None:
                    raise MotorError(f"Failed to re-read position for motor {self._motor_id}")
                pos = (pos_h << 16) | pos_l
                physical_pos = (pos / ppr) * self._params.mpr
                logical_pos = self._params.physical_to_logical(physical_pos)
                if abs(logical_pos) > max_valid:
                    self._logger.error(
                        "Garbage position persists after retry",
                        extra={"motor_id": self._motor_id, "logical_pos": logical_pos},
                    )
                    return 0.0

            self._cur_pos = logical_pos
            return logical_pos
            
        except Exception as e:
            if not isinstance(e, MotorError):
                raise MotorError(f"Failed to get position for motor {self._motor_id}: {str(e)}") from e
            raise
    
    # TODO add guards on setters and getters
    async def set_param(self, register: str, value: int) -> None:
        """Set a motor parameter register.
        
        Args:
            register: Name of the register to set
            value: Value to write to the register
            
        Raises:
            MotorError: If parameter write fails
            ValueError: If register name is invalid
        """
        try:
            reg_addr = motor_helpers.get_register_by_name(register)
            if reg_addr is None:
                raise ValueError(f"Invalid register name: {register}")
                
            await self._controller.write_single_register(
                self._motor_id,
                reg_addr,
                value
            )
            
            # if result is None:
            #     raise MotorError(f"Failed to write {value} to register {register}")
                
        except Exception as e:
            if not isinstance(e, (MotorError, ValueError)):
                raise MotorError(f"Failed to set parameter {register}: {str(e)}") from e
            raise
    
    async def get_param(self, register: str) -> Optional[int]:
        """Get a motor parameter register value.
        
        Args:
            register: Name of the register to read
            
        Raises:
            MotorError: If parameter read fails
            ValueError: If register name is invalid
            
        Returns:
            Register value as integer, or None if read fails
        """
        try:
            reg_addr = motor_helpers.get_register_by_name(register)
            if reg_addr is None:
                raise ValueError(f"Invalid register name: {register}")
                
            val = await self._controller.read_holding_registers(
                self._motor_id, 
                reg_addr
            )
            
            if val is not None:
                return int(val)
            raise MotorError(f"Failed to read register {register}")
            
        except Exception as e:
            if not isinstance(e, (MotorError, ValueError)):
                raise MotorError(f"Failed to get parameter {register}: {str(e)}") from e
            raise
    
    async def wait_for_move(self, timeout: float = MOTOR_WAIT_TIMEOUT, homing: bool = False) -> MotorStatus:
        """Wait for motor movement to complete.
        
        Args:
            timeout: Maximum time to wait in seconds
            
        Raises:
            MotorTimeoutError: If movement takes longer than timeout
            MotorError: If motor status read fails or motor faults
            
        Returns:
            MotorStatus indicating final state
        """
        start_time = time.monotonic()
        last_pos = None
        last_movement_time = start_time
        has_moved = False
        retry_count = 0

        self._logger.debug(
            "Waiting for motor motion",
            extra={"motor_id": self._motor_id, "timeout": timeout, "homing": homing},
        )
        while True:
            status_reg = getattr(self._params, "status_reg", 0x1003)
            status = await self._controller.read_holding_registers(self._motor_id, status_reg)
            if status is None:
                self._logger.error(
                    "Failed to read motion status from motor",
                    extra={"motor_id": self._motor_id},
                )
                raise MotorError(f"Failed to read motion status from motor {self._motor_id}")

            complete_bit = getattr(self._params, "status_complete_bit", 5)
            fault_bit_num = getattr(self._params, "status_fault_bit", 0)

            complete = status & (1 << complete_bit)
            fault_bit = (status & (1 << fault_bit_num)) if fault_bit_num is not None else 0

            await self.get_position()
            now = time.monotonic()

            if self._safety and self._safety.blocked:
                pause_started = time.monotonic()
                self._logger.info(
                    "Curtain guard stop",
                    extra={"motor_id": self._motor_id, "position_mm": self._cur_pos},
                )
                await self.stop()
                await self._safety.wait_clear()
                if self._pending_motion:
                    self._logger.info(
                        "Curtain guard resume",
                        extra={
                            "motor_id": self._motor_id,
                            "position_mm": self._cur_pos,
                            "paused_s": time.monotonic() - pause_started,
                        },
                    )
                    await self._pending_motion()
                start_time = time.monotonic()
                last_pos = None
                last_movement_time = start_time
                has_moved = False
                continue

            if last_pos is not None:
                if self._cur_pos != last_pos:
                    last_movement_time = now
                    has_moved = True

            elif last_movement_time is None:
                last_movement_time = now  # initialize once
            
            last_pos = self._cur_pos

            # Normal completion - only accept if motor moved or enough time passed
            # This prevents false completion from stale status bit
            elapsed = now - start_time
            if complete and (has_moved or elapsed > 0.5):
                await self.stop()
                self._pending_motion = None
                if self._target_pos is not None:
                    try:
                        current_pos = await self.get_position()
                        position_error = abs(current_pos - self._target_pos)
                        if position_error > POSITION_TOLERANCE_MM:
                            self._logger.warning(
                                "Motor stopped away from target",
                                extra={
                                    "motor_id": self._motor_id,
                                    "target_mm": self._target_pos,
                                    "position_mm": current_pos,
                                    "position_error_mm": position_error,
                                    "tolerance_mm": POSITION_TOLERANCE_MM,
                                },
                            )
                            self._target_pos = None
                            self._hard_stopped = True
                            self._is_homed = False
                            return MotorStatus.FAULT
                    except Exception as position_exc:
                        self._logger.warning(
                            "Failed to verify motor position after move",
                            extra={"motor_id": self._motor_id, "error": str(position_exc)},
                        )
                self._target_pos = None
                self._hard_stopped = False
                self._logger.debug(
                    "Motion complete",
                    extra={
                        "motor_id": self._motor_id,
                        "homing": homing,
                        "elapsed": elapsed,
                        "has_moved": has_moved,
                    }
                )
                return MotorStatus.STOPPED
            
            # Crash/alarm detection (follow-error, hardstop stall)
            # alarm_code 32 (0x20) means "stopped at limit" during homing, but collision during normal moves
            # only treat alarm_code 32 as collision if motor has actually started moving (not stale from previous op)
            alarm_code: Optional[int] = None
            alarm_active = False
            try:
                alarm_code = await self.get_param("Current alarm")
                if alarm_code is not None and alarm_code != 0:
                    if alarm_code == 32:
                        alarm_active = not homing and has_moved
                    else:
                        alarm_active = True
            except Exception as alarm_exc:
                self._logger.warning(
                    "Failed to read motor alarm register",
                    extra={"motor_id": self._motor_id, "error": str(alarm_exc)},
                )
                raise

            no_progress = (
                not has_moved and (now - start_time > max(2.0, self._stall_timeout * 6))
            )
            stall_detected = (
                has_moved and (now - last_movement_time > self._stall_timeout)
            )

            fault = bool(fault_bit or alarm_active or stall_detected or no_progress or self._simulated_fault)

            # Check for motor fault
            if fault:
                await self.stop()

                if homing:
                    self._target_pos = None
                    self._pending_motion = None
                    self._logger.warning(
                        "Fault/stall detected during homing (treated as stop)",
                        extra={
                            "motor_id": self._motor_id,
                            "elapsed": time.monotonic() - start_time,
                            "status_word": status,
                            "alarm_code": alarm_code,
                            "stall_detected": stall_detected,
                            "no_progress": no_progress,
                        },
                    )
                    return MotorStatus.STOPPED

                # For alarm_code 32 during normal moves, try to clear and resume once
                if alarm_code == 32 and not self._simulated_fault and retry_count < 1:
                    self._logger.warning(
                        "Alarm 32 detected, attempting clear and resume",
                        extra={
                            "motor_id": self._motor_id,
                            "position_mm": self._cur_pos,
                            "target_mm": self._target_pos,
                            "retry_count": retry_count,
                        },
                    )
                    try:
                        await self.set_param("Control word", 0x1111)
                        await self.set_param("Control word", 0x1122)
                        await self._controller.write_single_register(self._motor_id, 0x6000, 0x0000)
                        await asyncio.sleep(0.1)
                        retry_count += 1
                        last_movement_time = time.monotonic()
                        if self._pending_motion is not None:
                            await self._pending_motion()
                        continue
                    except Exception as retry_exc:
                        self._logger.warning(
                            "Failed to clear alarm and resume",
                            extra={"motor_id": self._motor_id, "error": str(retry_exc)},
                        )

                fault_type = "Simulated fault" if self._simulated_fault else "Fault"
                self._target_pos = None

                self._logger.warning(
                    f"{fault_type} detected - stopping motor, manual intervention required",
                    extra={
                        "motor_id": self._motor_id,
                        "homing": homing,
                        "status_word": status,
                        "position_mm": self._cur_pos,
                        "alarm_code": alarm_code,
                        "stall_detected": stall_detected,
                        "no_progress": no_progress,
                        "simulated": self._simulated_fault,
                    },
                )

                self._pending_motion = None
                self._hard_stopped = True
                self._is_homed = False
                return MotorStatus.FAULT


            # Check for timeout
            if time.monotonic() - start_time > timeout:
                await self.stop()
                self._pending_motion = None
                self._target_pos = None
                self._logger.error(
                    "Motion timeout",
                    extra={
                        "motor_id": self._motor_id,
                        "timeout": timeout,
                        "homing": homing,
                        "elapsed": time.monotonic() - start_time,
                    }
                )
                return MotorStatus.TIMEOUT

            await asyncio.sleep(0.01)


    async def home(self) -> None:
        """Home the motor to find its zero position.

        The homing direction register follows an inverted definition
        to the motor direction register, so this ends up being the
        opposite direction anyway.

        Raises:
            MotorConfigError: If homing configuration fails
            MotorError: If homing movement fails or motor faults
            MotorTimeoutError: If homing takes too long
        """
        self._simulated_fault = False

        try:
            sensor_cfg = self._get_home_sensor_config()
            if sensor_cfg is not None:
                await self._home_with_sensor(sensor_cfg)
                return

            home_dir = self._get_home_direction()

            try:
                home_rpm = self._params.home_rpm or self._params.ret_rpm
                await self._prepare_home_motion(home_rpm, home_dir)
            except Exception as e:
                raise MotorConfigError(f"Failed to configure homing parameters: {str(e)}") from e
            
            try:
                await self._start_path_zero()
            except Exception as e:
                raise MotorError(f"Failed to start homing movement: {str(e)}") from e

            t0 = time.monotonic()
            stop = await self.wait_for_move(timeout=MOTOR_HOME_TIMEOUT, homing=True)
            elapsed = time.monotonic() - t0

            if self._on_move_complete is not None:
                self._on_move_complete(elapsed)
            
            if stop == MotorStatus.STOPPED:
                try:
                    await self._set_zero_position()
                except Exception as e:
                    raise MotorError(f"Failed to set home position as zero: {str(e)}") from e
                
                await self.move_config()
                self._is_homed = True
                self._hard_stopped = False
            elif stop == MotorStatus.FAULT:
                self._is_homed = False
                self._hard_stopped = True
                raise MotorError(f"Motor {self._motor_id} faulted during homing")
            else:
                self._is_homed = False
                raise MotorTimeoutError(f"Motor {self._motor_id} homing timed out")
                
        except Exception as e:
            if isinstance(e, MotorTimeoutError):
                self._is_homed = False
            if not isinstance(e, (MotorError, MotorConfigError, MotorTimeoutError)):
                raise MotorError(f"Unexpected error during homing: {str(e)}") from e
            raise
    
    async def jog(self, dist: float) -> MotorStatus:
        """Move the motor by a relative distance.

        Args:
            dist: Distance to move in millimeters (logical)

        Returns:
            Final motor status

        Raises:
            MotorError: If movement fails
            MotorTimeoutError: If operation times out
        """
        try:
            physical_dist = self._params.logical_to_physical_delta(dist)

            ppr = getattr(self._params, "ppr", 10000)
            pulse = motor_helpers.rev_to_pulse(motor_helpers.mm_to_rev(physical_dist, self._params.mpr), ppr)
            
            # Configure for relative movement
            await self.set_param("Control word", 0x1111)  # clear alarm if any
            await self.set_param("Control word", 0x1122)  # clear alarm if any
            await self.set_param("Motion of Path 0", 0x41)  # relative position mode
            await self.set_param("Motor direction", self._params.dr)
            await self.set_param("Velocity", self._params.rpm)
            await self.set_param("Acceleration", self._params.accel)
            await self.set_param("Deceleration", self._params.accel)
            await self.set_param("Position High", pulse[0])
            await self.set_param("Position Low", pulse[1])
            
            # Start movement
            await self._controller.write_single_register(
                self._motor_id,
                motor_helpers.TRIGGER,
                0x10  # start path 0
            )

            t0 = time.monotonic()
            result = await self.wait_for_move(timeout=MOTOR_JOG_TIMEOUT, homing=True)
            elapsed = time.monotonic() - t0

            if self._on_move_complete is not None:
                self._on_move_complete(elapsed)

            if result != MotorStatus.STOPPED:
                raise MotorError(f"Relative move failed (status={result.name})")
            await self.move_config()
            return MotorStatus.STOPPED
            
        except Exception as e:
            if not isinstance(e, (MotorError, MotorTimeoutError)):
                raise MotorError(f"Relative move failed: {str(e)}") from e
            raise

    async def stop(self) -> MotorStatus:
        """Stop any current motor movement.

        Returns:
            Motor status after stopping

        Raises:
            MotorError: If stop command fails
        """
        try:
            await self._controller.write_single_register(
                self._motor_id,
                motor_helpers.TRIGGER,
                0x40  # stop command
            )
            return MotorStatus.STOPPED

        except Exception as e:
            raise MotorError(f"Failed to stop motor {self._motor_id}: {str(e)}") from e

    async def idle(self) -> None:
        """Stop and de-energize the motor when idle."""
        try:
            await self.stop()
        except Exception as e:
            self._logger.warning("Failed to idle motor: %s", e)

    async def ready(self) -> None:
        """Re-enable motor after idle/stop by clearing alarms and reconfiguring."""
        try:
            await self.clear_alarm()
        except Exception as e:
            self._logger.warning("Failed to ready motor: %s", e)

    def set_fault(self) -> None:
        self._simulated_fault = True
        self._status = MotorStatus.FAULT
        self._hard_stopped = True
        self._is_homed = False
        self._logger.warning(
            "Simulated fault triggered",
            extra={"motor_id": self._motor_id},
        )
