from typing import ClassVar, List

from pydantic import BaseModel, Field, RootModel, ValidationError

from hardware.controller import SharedModbusController
from hardware.drivers import hepg_register_types
from hardware.drivers.registers import (
    DeviceRegisters,
    DeviceRegistersModel,
)
from hardware.exceptions import (
    MotorConfigError,
    MotorError,
    MotorTimeoutError,
)

# --- HEPG DeviceRegister ---
REGISTERS_FILENAME = "../src/hardware/device_docs/hepg/HEPG.csv"
TYPES = DeviceRegisters.get_classes_in_module(hepg_register_types)
registers = DeviceRegisters.csv_to_registers(
    REGISTERS_FILENAME, TYPES
)
hepg_registers = DeviceRegisters(registers)


# --- HEPG DeviceRegisterModel ---
class HepgRegister(DeviceRegistersModel):
    REGISTERS: ClassVar[DeviceRegisters] = hepg_registers


class HepgRegisters(RootModel):
    # The 'root' attribute holds the list of Iclrs models
    root: List[HepgRegister]


# --- HEPG Config BaseModel ---
class HepgConfig(BaseModel):
    modbus_address: int = Field(default=9, ge=0)
    enabled: bool = Field(default=True)
    configuration_registers: HepgRegisters


# Seperate Servo and Stepper classes into dedicated files.
# Servo used for the end effector
class Hepg:
    """Controls a servo motor via Modbus."""

    def __init__(
        self,
        controller: SharedModbusController,
        params: HepgConfig,
    ):
        self._controller = controller
        self._config = params
        self._motor_id = params.modbus_address

    async def read_register(self, name: str) -> HepgRegister:
        """Get a motor parameter register value.

        Args:
            register: Name of the register to read

        Raises:
            MotorError: If parameter read fails
            ValueError: If register name is invalid

        Returns:
            Register value as integer, or None if read fails
        """
        # Get address from name
        try:
            register = HepgRegister(name=name)
        except ValidationError as e:
            raise ValueError(f"Invalid register name: {name}") from e
        address = register.address
        # Read address via Modbus
        try:
            value = await self._controller.read_holding_registers(
                self._motor_id, address
            )
        except Exception as e:
            if not isinstance(e, (MotorError, ValueError)):
                raise MotorError(
                    f"Failed to get parameter {register}: {str(e)}"
                ) from e
            raise

        if value is None:
            raise MotorError(f"Failed to read register {register}")
        else:
            return HepgRegister(name=name, value=value)

    async def write_register(self, name: str, value: Any) -> None:
        """Set a motor parameter register.

                Args:
            register: Name of the register to set
            value: Value to write to the register

        Raises:
            MotorError: If parameter write fails
            ValueError: If register name is invalid
        """

        # Get address from name
        try:
            register = HepgRegister(name=name, value=value)
        except ValidationError as e:
            raise ValueError(f"Invalid register: {e}") from e
        # Write register via modbus
        try:
            await self._controller.write_single_register(
                self._motor_id, register.modbus[0], register.modbus[1]
            )
        except Exception as e:
            if not isinstance(e, (MotorError, ValueError)):
                raise MotorError(
                    f"Failed to set parameter {register}: {str(e)}"
                ) from e
            raise

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
                0x0,  # clear
            )
            await self._controller.write_single_register(
                self._motor_id,
                self._control_reg,
                0x1,  # enable
            )
            await self.wait_for_activate()

            # Static position settings
            # Open
            await self._controller.write_single_register(
                self._motor_id,
                0x03EB,
                0x8020,  # 0x80|0x20
            )
            # Close
            await self._controller.write_single_register(
                self._motor_id,
                0x03ED,
                0x80FE,  # 0x80|0xFE
            )
            # Safe open
            await self._controller.write_single_register(
                self._motor_id,
                0x03EF,
                0x80C0,  # 0x80|0xFF
            )

            # Set configuration values
            await self._controller.write_single_register(
                self._motor_id, 0x03EC, 0x80
            )
            await self._controller.write_single_register(
                self._motor_id, 0x03EE, 0x80
            )
            await self._controller.write_single_register(
                self._motor_id, 0x03F0, 0x80
            )

        except Exception as e:
            raise MotorError(
                f"Failed to configure servo: {str(e)}"
            ) from e

    async def wait_for_activate(self):
        """Wait for motor to activate.

        Raises:
            MotorTimeoutError: If activation takes too long
            MotorError: If motor status read fails
        """
        start_time = time.monotonic()

        while True:
            status = await self._controller.read_holding_registers(
                self._motor_id, self._status_reg
            )
            if status is None:
                raise MotorError(
                    "Failed to read motor status during activation"
                )

            # TODO: DEFINE USING A BITFIELD/BITMASK
            activation_status = status & (1 << 5)
            if activation_status:
                return

            if (
                time.monotonic() - start_time
                > MOTOR_ACTIVATION_TIMEOUT_S
            ):
                raise MotorTimeoutError(
                    "Motor activation timed out after 3 seconds"
                )

            await asyncio.sleep(0.01)

    async def wait_for_servo(
        self, timeout: float = 5.0
    ) -> MotorStatus:
        """Wait for servo to complete its motion.

        Args:
            timeout: Maximum time to wait in seconds

        Raises:
            MotorTimeoutError: If servo motion takes longer than timeout
            MotorError: If motor status read fails
        """
        start_time = time.monotonic()

        while True:
            status = await self._controller.read_holding_registers(
                self._motor_id, self._status_reg
            )
            if status is None:
                raise MotorError(
                    "Failed to read motor status during servo motion"
                )

            move_status = (status & (1 << 6)) or (status & (1 << 7))
            if move_status:
                return MotorStatus.STOPPED

            if time.monotonic() - start_time > timeout:
                raise MotorTimeoutError(
                    f"Servo motion timed out after {timeout} seconds"
                )

            await asyncio.sleep(0.01)

    async def open(self):
        """Open the servo to maximum position.

        Raises:
            MotorError: If servo control fails
            MotorTimeoutError: If operation times out
        """
        try:
            await self._controller.write_single_register(
                self._motor_id, self._control_reg, 0x080B
            )
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
            await self._controller.write_single_register(
                self._motor_id, self._control_reg, 0x0A0B
            )
            await self.wait_for_servo()
        except Exception as e:
            raise MotorError(
                f"Failed to safe-open servo: {str(e)}"
            ) from e

    async def close(self):
        await self._controller.write_single_register(
            self._motor_id, self._control_reg, 0x090B
        )
        await self.wait_for_servo()
