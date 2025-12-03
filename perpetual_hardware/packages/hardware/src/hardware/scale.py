from __future__ import annotations

import asyncio
from typing import Optional, Dict, Any, Tuple

from hardware.controller import SharedModbusController


class Scale:
    """
    Async driver for Ruishan scale using SharedModbusController (pymodbus async).

    Register map (0-based holding registers):
      0 -> 40001: Weight register 1
      1 -> 40002: Weight register 2
      2 -> 40003: Decimal position (0..3)
      3 -> 40004: Status bits: bit0=zero, bit1=stable, bit2=overload
      4 -> 40005: Tare (write 1)

    Weight calculation: abs(reg0 - reg1) / 10^decimals
    """

    REG_WEIGHT_1 = 0
    REG_WEIGHT_2 = 1
    REG_DECIMALS = 2
    REG_STATUS = 3
    REG_TARE = 4

    def __init__(
        self,
        modbus_controller: SharedModbusController,
        scale_id: int = 1,
        retries: int = 2,
        retry_delay: float = 0.05,
    ):
        self.modbus_controller = modbus_controller
        self.scale_id = scale_id
        self.retries = max(0, retries)
        self.retry_delay = retry_delay

    # ---------- connection mgmt ----------

    async def connect(self) -> bool:
        await self.modbus_controller.connect()
        return bool(getattr(self.modbus_controller.client, "connected", True))

    async def close(self) -> None:
        await self.modbus_controller.close()

    # ---------- helpers ----------

    async def _read_one(self, addr: int) -> Optional[int]:
        """Read a single holding register with retries."""
        for _ in range(self.retries + 1):
            val = await self.modbus_controller.read_holding_registers(self.scale_id, addr, 1)
            if isinstance(val, int):
                return val & 0xFFFF
            await asyncio.sleep(self.retry_delay)
        return None

    async def _read_block(self, start: int, count: int) -> Optional[Tuple[int, ...]]:
        """
        Read 'count' registers starting at 'start'.
        (Controller returns only first register even if count>1, so read individually.)
        """
        vals = []
        for ofs in range(count):
            v = await self._read_one(start + ofs)
            if v is None:
                return None
            vals.append(v)
        return tuple(vals)

    @staticmethod
    def _interpret_status(v: int) -> Dict[str, bool | int]:
        return {
            "zero": bool(v & (1 << 0)),
            "stable": bool(v & (1 << 1)),
            "overload": bool(v & (1 << 2)),
            "raw": v & 0xFFFF,
        }

    # ---------- high-level API ----------

    async def read_weight(self, averages: int = 1, delay: float = 0.1) -> Optional[float]:
        """
        Return current weight in grams (absolute value for inverted mount).
        Optionally average N samples.
        """
        averages = max(1, int(averages))
        total = 0.0
        got = 0

        for i in range(averages):
            regs = await self._read_block(self.REG_WEIGHT_1, 3)
            if regs is None:
                if i < averages - 1:
                    await asyncio.sleep(delay)
                    continue
                return None
            w1, w2, decimals = regs
            raw = abs(w1 - w2)
            grams = raw / (10 ** int(decimals))
            total += grams
            got += 1
            if i < averages - 1:
                await asyncio.sleep(delay)

        return (total / got) if got else None

    async def read_weight_data(self) -> Dict[str, Any]:
        """
        Rich read: weight (grams), decimals, status flags, and raw regs.
        """
        regs = await self._read_block(self.REG_WEIGHT_1, 4)
        if regs is None:
            raise RuntimeError("Failed to read weight registers")
        w1, w2, decimals, status_raw = regs
        raw = abs(w1 - w2)
        grams = raw / (10 ** int(decimals))
        return {
            "weight": grams,
            "decimal_position": int(decimals),
            "status": self._interpret_status(status_raw),
            "raw_registers": {
                "weight_1": w1,
                "weight_2": w2,
                "decimals": decimals,
                "status": status_raw,
            },
        }

    async def tare(self) -> bool:
        """Tare: write 1 to 40005 (address 4)."""
        ok = await self.modbus_controller.write_single_register(self.scale_id, self.REG_TARE, 1)
        return ok is None or ok is True  # your controller returns None on success

    async def read_status(self) -> Dict[str, Any]:
        v = await self._read_one(self.REG_STATUS)
        if v is None:
            raise RuntimeError("Failed to read status register")
        return self._interpret_status(v)

    async def wait_for_stable_weight(
        self,
        timeout: float = 2.0,
        poll_interval: float = 0.05,
    ) -> Optional[float]:
        """
        Poll status until stable, then return one weight sample in grams.
        Returns None on timeout.
        """
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout

        while loop.time() < deadline:
            st = await self.read_status()
            if st["stable"] is True:
                return await self.read_weight()
            await asyncio.sleep(poll_interval)

        return None
