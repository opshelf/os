from hardware import motor_helpers
from hardware.controller import SharedModbusController
import time
import asyncio
from enum import Enum
from typing import Any, Optional

from hardware.exceptions import (
    MotorError, MotorTimeoutError, MotorLimitError,
    MotorConfigError, ConfigurationError
)

class MotorStatus(Enum):
    IDLE = 0
    MOVING = 1
    STOPPED = 2
    ERROR = 3

class Servo:
    """Controls a servo motor via Modbus."""

    def __init__(self, controller: SharedModbusController):
        """Initialize servo controller.
        
        Args:
            controller: Shared Modbus controller instance
        """
        self._motor_id = 0x9                  # slave id
        self._controller = controller
        self._control_reg = 0x3E8
        self._status_reg = 0x07D0            # status register

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

    def __init__(self, controller: SharedModbusController, params: Any) -> None:
        self._motor_id = params.motor_id        
        self._controller = controller           
        self._params = params
        self._cur_pos = None
        self._stall_timeout = 0.25                     
        self._status = MotorStatus.IDLE

    async def configure(self):
        """Configure the stepper motor with initial parameters.
        
        Raises:
            MotorConfigError: If any configuration parameter fails to set
            MotorError: If communication with motor fails
        """
        try:
            # Clear any existing alarms
            await self.set_param("Control word", 0x1111)
            await self.set_param("Control word", 0x1122)
            
            # Configure motor parameters
            await self.set_param("SO2 output function", 0xA1)  # brake output
            await self.set_param("Acceleration", self._params.accel)
            await self.set_param("Deceleration", self._params.accel)
            await self.set_param("Peak current", self._params.Ip)
            
            # Configure movement parameters
            await self.move_config()
        except Exception as e:
            raise MotorConfigError(f"Failed to configure motor {self._motor_id}: {str(e)}") from e

    # TODO add axis limits
    async def move_to_position(self, value: float) -> None:  # mm
        """Move the motor to an absolute position in millimeters.
        
        Args:
            value: Target position in millimeters
            
        Raises:
            MotorLimitError: If target position exceeds motor limits
            MotorError: If movement fails or times out
        """
        if value > self._params.lim:
            raise MotorLimitError(
                f"Target position {value}mm exceeds motor limit {self._params.lim}mm"
            )
            
        try:
            # Convert mm to pulses
            pulse = motor_helpers.rev_to_pulse(motor_helpers.mm_to_rev(value, self._params.mpr))
            
            # Set target position
            await self.set_param("Position High", pulse[0])
            await self.set_param("Position Low", pulse[1])
            
            # Start movement
            await self._controller.write_single_register(
                self._motor_id, 
                motor_helpers.TRIGGER,
                0x10  # start path 0
            )
            
            self._status = MotorStatus.MOVING
            status = await self.wait_for_move(timeout=10)
            
            if status == MotorStatus.ERROR:
                raise MotorError(f"Movement failed for motor {self._motor_id}")
                
        except Exception as e:
            if not isinstance(e, MotorError):
                raise MotorError(f"Failed to move motor {self._motor_id} to position {value}mm: {str(e)}") from e
            raise
    
    async def move_to_position_no_wait(self, value: float) -> None:
        """Move motor to position without waiting for completion.
        
        Args:
            value: Target position in millimeters
            
        Raises:
            MotorLimitError: If target position exceeds motor limits
            MotorError: If movement setup fails
        """
        if value > self._params.lim:
            raise MotorLimitError(
                f"Target position {value}mm exceeds motor limit {self._params.lim}mm"
            )
            
        try:
            # Convert mm to pulses
            pulse = motor_helpers.rev_to_pulse(motor_helpers.mm_to_rev(value, self._params.mpr))
            
            # Set target position
            await self.set_param("Position High", pulse[0])
            await self.set_param("Position Low", pulse[1])
            
            # Start movement
            await self._controller.write_single_register(
                self._motor_id, 
                motor_helpers.TRIGGER,
                0x10  # start path 0
            )
            
            self._status = MotorStatus.MOVING
            
        except Exception as e:
            if not isinstance(e, (MotorError, MotorLimitError)):
                raise MotorError(f"Failed to start movement to {value}mm: {str(e)}") from e
            raise
    
    async def move_config(self) -> None:
        """Configure motor movement parameters.
        
        Raises:
            MotorConfigError: If configuration fails
        """
        try:
            # Clear any alarms
            await self.set_param("Control word", 0x1111)
            await self.set_param("Control word", 0x1122)
            
            # Configure movement parameters
            await self.set_param("Motion of Path 0", 0x1)  # abs pos 0x1
            await self.set_param("Motor direction", self._params.dr)
            await self.set_param("Velocity", self._params.rpm)
            
        except Exception as e:
            raise MotorConfigError(f"Failed to configure movement parameters: {str(e)}") from e
        
    #TODO return val in mm, not pulsesat:
    async def get_position(self) -> float:
        """Get the current motor position in millimeters.
        
        Raises:
            MotorError: If position read fails or calculation fails
            
        Returns:
            Current position in millimeters
        """
        try:
            # Read position registers
            pos_l = await self.get_param("Position Low (Actual)") 
            pos_h = await self.get_param("Position High (Actual)")
            
            if pos_l is None or pos_h is None:
                raise MotorError(f"Failed to read position registers for motor {self._motor_id}")
            
            # Combine high and low position values
            pos = (pos_h << 16) | pos_l
            if not pos:
                return -1
                
            # Get pulses per revolution
            ppr = await self.get_param("Pulse/revolution")
            if not ppr:
                DEFAULT_PULSES_PER_REV = 10000
                ppr = DEFAULT_PULSES_PER_REV
                
            # Convert to millimeters
            pos = (pos/ppr) * self._params.mpr
            self._cur_pos = pos
            return pos
            
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
                
            result = await self._controller.write_single_register(
                self._motor_id, 
                reg_addr,
                value
            )
            
            if result is None:
                raise MotorError(f"Failed to write {value} to register {register}")
                
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
    
    #TODO add checks for position following error (crash detection)
    async def wait_for_move(self, timeout: float = 2.5) -> MotorStatus:
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
        has_moved = False
        last_movement_time = start_time

        while True:
            status = await self.get_param("Motion status")
            if status is None:
                raise MotorError(f"Failed to read motion status from motor {self._motor_id}")
            
            complete = status & (1 << 5)
            fault = status & 1

            await self.get_position()
            now = time.monotonic()
            
            if last_pos is not None:
                if(self._cur_pos != last_pos):
                    last_movement_time = now
                    has_moved = True
                
            elif last_movement_time is None:
                last_movement_time = now  # initialize once
            
            last_pos = self._cur_pos
            
            # Check for stall condition
            if has_moved and (now - last_movement_time > self._stall_timeout):
                await self.stop()
                raise MotorError(f"Motor {self._motor_id} stalled")

            # Normal completion
            if complete:
                await self.stop()
                return MotorStatus.STOPPED
            
            # Check for motor fault
            if fault:
                await self.stop()
                raise MotorError(f"Motor {self._motor_id} reported fault condition")

            # Check for timeout
            if time.monotonic() - start_time > timeout:
                await self.stop()
                raise MotorTimeoutError(
                    f"Motor {self._motor_id} movement timed out after {timeout} seconds"
                )

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
        try:
            # Calculate homing direction (inverted from normal direction)
            home_dir = int(self._params.dr)^1
            
            try:
                # Configure homing parameters
                await self.set_param("Control word", 0x1111)  # clear alarm if any
                await self.set_param("Control word", 0x1122)  # clear alarm if any
                await self.set_param("Motion of Path 0", 0x2)  # velocity mode
                await self.set_param("Motor direction", home_dir)
                await self.set_param("Velocity", self._params.ret_rpm)
            except Exception as e:
                raise MotorConfigError(f"Failed to configure homing parameters: {str(e)}") from e
            
            try:
                # Start homing movement
                await self._controller.write_single_register(
                    self._motor_id, 
                    motor_helpers.TRIGGER,
                    0x10  # start path 0
                )
                
                self._status = MotorStatus.MOVING
            except Exception as e:
                raise MotorError(f"Failed to start homing movement: {str(e)}") from e

            # Wait for homing to complete (longer timeout for homing)
            stop = await self.wait_for_move(timeout=60)
            
            if stop == MotorStatus.STOPPED:
                try:
                    # Set current position as zero
                    await self._controller.write_single_register(
                        self._motor_id,
                        motor_helpers.TRIGGER,
                        0x21  # set zero position
                    )
                except Exception as e:
                    raise MotorError(f"Failed to set home position as zero: {str(e)}") from e
                
                # Reconfigure for normal movement
                await self.move_config()
            else:
                raise MotorError(f"Motor {self._motor_id} did not stop properly after homing")
                
        except Exception as e:
            if not isinstance(e, (MotorError, MotorConfigError, MotorTimeoutError)):
                raise MotorError(f"Unexpected error during homing: {str(e)}") from e
            raise
    
    async def jog(self, dist: float) -> MotorStatus:
        """Move the motor by a relative distance.
        
        Args:
            dist: Distance to move in millimeters
            
        Returns:
            Final motor status
            
        Raises:
            MotorError: If movement fails
            MotorTimeoutError: If operation times out
        """
        try:
            # Convert to pulses
            pulse = motor_helpers.rev_to_pulse(motor_helpers.mm_to_rev(dist, self._params.mpr))
            
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
            
            # Wait for completion
            await self.wait_for_move(timeout=10)
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
