from perpetual_os1.gripper.homwing.registers import ControlRegisters, StatusRegisters
from pymodbus.client.serial import AsyncModbusSerialClient
from typing import List
from perpetual_os1.gripper.homwing.core import (
    ExecuteControlMovement,
    GripperStatus,
    GripperFaultStatus,
    VoltageTemperature,
    SpeedForce,
    FaultPosition,
    ControlRegister,
    GOBJ,
    GSTA,
)
import asyncio


class HomwingGripper:
    def __init__(self, client: AsyncModbusSerialClient, address: int) -> None:
        self._client = client
        self._address = address

    @property
    def address(self) -> int:
        return self._address

    @address.setter
    def address(self, value: int) -> None:
        if value > 247:
            raise ValueError("Invalid Address")
        self._address = value

    async def read_gripper_status(self) -> GripperStatus:
        result = await self._read_gripper_status()
        return GripperStatus.from_u8(result[0] & 0xFF)

    async def _read_gripper_status(self) -> List[int]:
        result = await self._client.read_input_registers(
            StatusRegisters.STATUS, count=2, slave=self._address
        )
        return result.registers

    async def read_fault_position(self) -> FaultPosition:
        result = await self._read_fault_position()
        return FaultPosition(
            fault=GripperFaultStatus.from_u8(result[0] & 0xFF),
            position=result[0] >> 8 & 0xFF,
        )

    async def _read_fault_position(self) -> List[int]:
        result = await self._client.read_input_registers(
            StatusRegisters.FAULT_POSITION_STATUS, count=1, slave=self._address
        )
        return result.registers

    async def read_speed_force(self) -> SpeedForce:
        result = await self._read_speed_force()
        return SpeedForce(speed=result[0] & 0xFF, force=result[0] >> 8 & 0xFF)

    async def _read_speed_force(self) -> List[int]:
        result = await self._client.read_input_registers(
            StatusRegisters.SPEED_FORCE_STATUS, count=1, slave=self._address
        )
        return result.registers

    async def read_voltage_temperature(self) -> VoltageTemperature:
        result = await self._read_voltage_temperature()
        return VoltageTemperature(voltage=result[0] & 0xFF, temperature=result[0] >> 8 & 0xFF)

    async def _read_voltage_temperature(self) -> List[int]:
        result = await self._client.read_input_registers(
            StatusRegisters.VOLTAGE_TEMPERATURE_STATUS, count=1, slave=self._address
        )
        return result.registers

    async def read_software_version(self) -> List[int]:
        result = await self._client.read_input_registers(
            StatusRegisters.SOFTWARE_INFO, count=1, slave=self._address
        )
        return result.registers

    async def write_control_register(self, control: ControlRegister) -> None:
        await self._client.write_registers(
            ControlRegisters.CONTROL, [control.to_u8()], slave=self._address
        )

    async def clear_control_register(self) -> None:
        await self.write_control_register(
            ControlRegister(
                enable=False,
                mode=False,
                move_to_targret=False,
                automatic_inspection=False,
                automatic_patrol_inspection=False,
            )
        )

    async def activate(self) -> None:
        await self.write_control_register(
            ControlRegister(
                enable=True,
                mode=False,
                move_to_targret=False,
                automatic_inspection=False,
                automatic_patrol_inspection=False,
            )
        )

    async def wait_for_move(self) -> None:
        while True:
            result = await self.read_gripper_status()
            if result.gOBJ != GOBJ.MOVING_TO_POSITION:
                break
            await asyncio.sleep(0.1)

    async def move_to_position(self, position: int) -> None:
        await self._set_target_position(position)
        await self.execute_move()

    async def set_target_position(self, position: int) -> None:
        await self._set_target_position(position)

    async def _set_target_position(self, position: int) -> None:
        await self._client.write_registers(
            ControlRegisters.POSITION, [position << 8], slave=self._address
        )

    async def set_speed_force(self, speed: int, force: int) -> None:
        await self._set_speed_force(speed, force)

    async def _set_speed_force(self, speed: int, force: int) -> None:
        await self._client.write_registers(
            ControlRegisters.SPEED_FORCE, [speed + (force << 8)], slave=self._address
        )

    async def execute_control_movement(self, movement: ExecuteControlMovement) -> None:
        await self.set_speed_force(movement.speed, movement.force)
        await self.move_to_position(movement.position)
        await self.execute_move()

    async def stop(self) -> None:
        await self.write_control_register(
            ControlRegister(
                enable=True,
                mode=False,
                move_to_targret=False,
                automatic_inspection=False,
                automatic_patrol_inspection=False,
            )
        )

    async def execute_move(self) -> None:
        await self.write_control_register(
            ControlRegister(
                enable=True,
                mode=False,
                move_to_targret=True,
                automatic_inspection=False,
                automatic_patrol_inspection=False,
            )
        )

    async def wait_for_activation(self) -> None:
        async with asyncio.timeout(5):
            while True:
                result = await self.read_gripper_status()
                if result.gSTA == GSTA.ACTIVATED:
                    break
                await asyncio.sleep(0.1)
