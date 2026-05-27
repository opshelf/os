from __future__ import annotations

import logging
from typing import Optional
from hardware.controller import SharedModbusController

logger = logging.getLogger(__name__).getChild("laser")


class LaserSensor:
    def __init__(
        self,
        modbus_controller: SharedModbusController,
        device_id: int,
        read_function_code: int,
        register_address: int,
        register_count: int,
        distance_register_index: int,
        distance_scale_mm: float,
    ) -> None:
        if register_count <= 0:
            raise ValueError("register_count must be greater than zero")

        if distance_register_index < 0 or distance_register_index >= register_count:
            raise ValueError("distance_register_index must be within register_count")

        if distance_scale_mm <= 0:
            raise ValueError("distance_scale_mm must be greater than zero")

        if read_function_code not in (3, 4):
            raise ValueError("read_function_code must be 3 or 4")

        self.modbus_controller = modbus_controller
        self.device_id = device_id
        self.read_function_code = read_function_code
        self.register_address = register_address
        self.register_count = register_count
        self.distance_register_index = distance_register_index
        self.distance_scale_mm = distance_scale_mm

    async def connect(self) -> bool:
        logger.debug(
            "Connecting to laser controller",
            extra={
                "unit_id": self.device_id,
                "port": getattr(self.modbus_controller, "port", None),
                "baudrate": getattr(self.modbus_controller, "baudrate", None),
            },
        )

        await self.modbus_controller.connect()

        return bool(getattr(self.modbus_controller.client, "connected", True))

    async def close(self) -> None:
        logger.debug("Closing laser controller", extra={"unit_id": self.device_id})
        await self.modbus_controller.close()

    async def read_registers(self) -> tuple[int, ...]:
        if self.read_function_code == 4:
            registers = await self.modbus_controller.read_input_register_block(
                unit_id=self.device_id,
                address=self.register_address,
                count=self.register_count,
            )
        else:
            registers = await self.modbus_controller.read_holding_register_block(
                unit_id=self.device_id,
                address=self.register_address,
                count=self.register_count,
            )

        logger.debug(
            "Laser registers read",
            extra={
                "unit_id": self.device_id,
                "function_code": self.read_function_code,
                "register_address": self.register_address,
                "register_count": self.register_count,
                "registers": list(registers),
            },
        )

        return registers

    async def read_distance_mm(self) -> float:
        registers = await self.read_registers()
        raw_distance = registers[self.distance_register_index]
        distance_mm = float(raw_distance) * self.distance_scale_mm

        logger.debug(
            "Laser distance read",
            extra={
                "unit_id": self.device_id,
                "raw_distance": raw_distance,
                "distance_mm": distance_mm,
            },
        )

        return distance_mm

    async def initialize(self) -> float:
        distance_mm = await self.read_distance_mm()

        logger.info(
            "Laser sensor initialized",
            extra={
                "unit_id": self.device_id,
                "distance_mm": distance_mm,
            },
        )

        return distance_mm

    async def read_distance_mm_or_none(self) -> Optional[float]:
        try:
            return await self.read_distance_mm()
        except Exception:
            logger.warning(
                "Laser distance read failed",
                extra={"unit_id": self.device_id},
                exc_info=True,
            )

            return None
