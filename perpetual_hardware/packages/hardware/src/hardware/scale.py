from __future__ import annotations

import asyncio
import logging
from typing import Optional, Dict, Any, Tuple

from hardware.controller import SharedModbusController

logger = logging.getLogger(__name__).getChild("scale")


class Scale:
    """
    Async driver for SJ-101D_X using your SharedModbusController (pymodbus async).

    Register map (0-based holding registers):
      0 -> 40001: Weight high 16 bits
      1 -> 40002: Weight low  16 bits
      2 -> 40003: Decimal position (0..3)
      3 -> 40004: Status bits: bit0=stable, bit1=zero, bit2=overload, bit4=valid
      4 -> 40005: Tare (write 1)
    """

    REG_WEIGHT_HI = 0
    REG_WEIGHT_LO = 1
    REG_DECIMALS = 2
    REG_STATUS = 3
    REG_TARE = 4
    REG_FULL_SCALE_HI = 25
    REG_FULL_SCALE_LO = 26
    REG_CAL_WEIGHT_HI = 27
    REG_CAL_WEIGHT_LO = 28
    REG_PRECISION = 29
    REG_DIVISION = 30
    REG_CAL_ZERO = 31
    REG_CAL_WEIGHT = 32
    REG_CAL_STATUS = 33

    CAL_STATUS_ZERO_OK = 0x04
    CAL_STATUS_WEIGHT_OK = 0x08

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
        logger.debug(
            "Connecting to scale controller",
            extra={
                "unit_id": self.scale_id,
                "port": getattr(self.modbus_controller, "port", None),
                "baudrate": getattr(self.modbus_controller, "baudrate", None),
            },
        )
        await self.modbus_controller.connect()
        return bool(getattr(self.modbus_controller.client, "connected", True))

    async def close(self) -> None:
        logger.debug("Closing scale controller", extra={"unit_id": self.scale_id})
        await self.modbus_controller.close()

    # ---------- helpers ----------

    async def _read_one(self, addr: int) -> Optional[int]:
        """Read a single holding register with retries."""
        for _ in range(self.retries + 1):
            val = await self.modbus_controller.read_holding_registers(self.scale_id, addr, 1)
            if isinstance(val, int):
                return val & 0xFFFF
            await asyncio.sleep(self.retry_delay)
        logger.warning(
            "Scale register read failed",
            extra={"unit_id": self.scale_id, "address": addr, "retries": self.retries},
        )
        return None

    async def _read_block(self, start: int, count: int) -> Optional[Tuple[int, ...]]:
        """
        Read 'count' registers starting at 'start'.
        (Your controller returns only first register even if count>1, so read individually.)
        """
        vals = []
        for ofs in range(count):
            v = await self._read_one(start + ofs)
            if v is None:
                logger.warning(
                    "Scale block read failed",
                    extra={
                        "unit_id": self.scale_id,
                        "start_address": start,
                        "count": count,
                        "offset_failed": ofs,
                    },
                )
                return None
            vals.append(v)
        return tuple(vals)

    @staticmethod
    def _to_int32_signed(hi: int, lo: int) -> int:
        raw = ((hi & 0xFFFF) << 16) | (lo & 0xFFFF)
        return raw - 0x1_0000_0000 if (raw & 0x8000_0000) else raw

    @staticmethod
    def _interpret_status(v: int) -> Dict[str, bool | int]:
        return {
            "stable":   bool(v & (1 << 0)),
            "zero":     bool(v & (1 << 1)),
            "overload": bool(v & (1 << 2)),
            "valid":    bool(v & (1 << 4)),
            "raw":      v & 0xFFFF,
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
            regs = await self._read_block(self.REG_WEIGHT_HI, 3)
            if regs is None:
                if i < averages - 1:
                    await asyncio.sleep(delay)
                    continue
                logger.warning(
                    "Scale weight read failed after retries",
                    extra={
                        "unit_id": self.scale_id,
                        "samples_requested": averages,
                        "delay_s": delay,
                    },
                )
                return None
            hi, lo, decimals = regs
            raw = self._to_int32_signed(hi, lo)
            grams = abs(raw * (10 ** (-int(decimals))))
            total += grams
            got += 1
            if i < averages - 1:
                await asyncio.sleep(delay)

        logger.debug(
            "Scale weight read",
            extra={
                "unit_id": self.scale_id,
                "averages": averages,
                "value_grams": (total / got) if got else None,
            },
        )
        return (total / got) if got else None

    async def read_weight_data(self) -> Dict[str, Any]:
        """
        Rich read: weight (grams), decimals, status flags, and raw regs.
        Note: This method returns raw values without abs() - used for debugging.
        """
        regs = await self._read_block(self.REG_WEIGHT_HI, 4)  # hi, lo, decimals, status
        if not regs:
            logger.error(
                "Failed to read weight registers",
                extra={"unit_id": self.scale_id},
            )
            raise RuntimeError("Failed to read weight registers")
        hi, lo, decimals, status_raw = regs
        raw = self._to_int32_signed(hi, lo)
        kg = raw * (10 ** (-int(decimals)))
        return {
            "weight": kg,
            "decimal_position": int(decimals),
            "status": self._interpret_status(status_raw),
            "raw_registers": {
                "weight_hi": hi,
                "weight_lo": lo,
                "decimals": decimals,
                "status": status_raw,
            },
        }

    async def tare(self) -> bool:
        """Tare: write 1 to 40005 (address 4)."""
        ok = await self.modbus_controller.write_single_register(self.scale_id, self.REG_TARE, 1)
        success = ok is None or ok is True  # your controller returns None on success
        if not success:
            logger.warning(
                "Scale tare failed",
                extra={"unit_id": self.scale_id},
            )
        else:
            logger.debug("Scale tare command sent", extra={"unit_id": self.scale_id})
        return success

    async def read_status(self) -> Dict[str, Any]:
        v = await self._read_one(self.REG_STATUS)
        if v is None:
            logger.error(
                "Failed to read scale status register",
                extra={"unit_id": self.scale_id},
            )
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
                weight = await self.read_weight()
                logger.debug(
                    "Scale reached stable reading",
                    extra={"unit_id": self.scale_id, "weight_grams": weight},
                )
                return weight
            await asyncio.sleep(poll_interval)

        logger.warning(
            "Scale stability wait timed out",
            extra={
                "unit_id": self.scale_id,
                "timeout_s": timeout,
                "poll_interval_s": poll_interval,
            },
        )
        return None

    async def _write_one(self, addr: int, value: int) -> bool:
        try:
            await self.modbus_controller.write_single_register(self.scale_id, addr, value)
            return True
        except Exception as exc:
            logger.warning(
                "Scale register write failed",
                extra={
                    "unit_id": self.scale_id,
                    "address": addr,
                    "value": value,
                    "error": str(exc),
                },
                exc_info=True,
            )
            return False

    async def get_calibration_status(self) -> Dict[str, Any]:
        v = await self._read_one(self.REG_CAL_STATUS)
        if v is None:
            logger.error(
                "Failed to read calibration status",
                extra={"unit_id": self.scale_id},
            )
            raise RuntimeError("Failed to read calibration status register")
        return {
            "zero_ok": bool(v & self.CAL_STATUS_ZERO_OK),
            "weight_ok": bool(v & self.CAL_STATUS_WEIGHT_OK),
            "raw": v,
        }

    async def get_calibration_params(self) -> Dict[str, Any]:
        full_scale = await self._read_block(self.REG_FULL_SCALE_HI, 2)
        cal_weight = await self._read_block(self.REG_CAL_WEIGHT_HI, 2)
        precision = await self._read_one(self.REG_PRECISION)
        division = await self._read_one(self.REG_DIVISION)

        if full_scale is None or cal_weight is None or precision is None or division is None:
            logger.error(
                "Failed to read calibration parameters",
                extra={"unit_id": self.scale_id},
            )
            raise RuntimeError("Failed to read calibration parameters")

        full_scale_val = self._to_int32_signed(full_scale[0], full_scale[1])
        cal_weight_val = self._to_int32_signed(cal_weight[0], cal_weight[1])

        return {
            "full_scale": full_scale_val,
            "calibration_weight": cal_weight_val,
            "precision": precision,
            "division": division,
        }

    async def set_calibration_params(
        self,
        full_scale: int,
        calibration_weight: int,
        precision: int = 1,
        division: int = 1,
    ) -> bool:
        full_scale_hi = (full_scale >> 16) & 0xFFFF
        full_scale_lo = full_scale & 0xFFFF
        cal_weight_hi = (calibration_weight >> 16) & 0xFFFF
        cal_weight_lo = calibration_weight & 0xFFFF

        ok = True
        ok = ok and await self._write_one(self.REG_FULL_SCALE_HI, full_scale_hi)
        ok = ok and await self._write_one(self.REG_FULL_SCALE_LO, full_scale_lo)
        ok = ok and await self._write_one(self.REG_CAL_WEIGHT_HI, cal_weight_hi)
        ok = ok and await self._write_one(self.REG_CAL_WEIGHT_LO, cal_weight_lo)
        ok = ok and await self._write_one(self.REG_PRECISION, precision)
        ok = ok and await self._write_one(self.REG_DIVISION, division)
        logger.info(
            "Scale calibration parameters updated",
            extra={
                "unit_id": self.scale_id,
                "full_scale": full_scale,
                "calibration_weight": calibration_weight,
                "precision": precision,
                "division": division,
                "success": ok,
            },
        )
        return ok

    async def calibrate_zero(self, timeout: float = 5.0) -> Dict[str, Any]:
        ok = await self._write_one(self.REG_CAL_ZERO, 1)
        if ok is False:
            logger.warning(
                "Failed to trigger zero calibration",
                extra={"unit_id": self.scale_id},
            )
            return {"success": False, "error": "Failed to write zero calibration command"}

        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout

        while loop.time() < deadline:
            await asyncio.sleep(0.2)
            status = await self.get_calibration_status()
            if status["zero_ok"] is True:
                return {"success": True, "status": status}

        logger.warning(
            "Zero calibration timed out",
            extra={"unit_id": self.scale_id, "timeout_s": timeout},
        )
        return {"success": False, "error": "Zero calibration timeout", "status": await self.get_calibration_status()}

    async def calibrate_weight(self, timeout: float = 5.0) -> Dict[str, Any]:
        ok = await self._write_one(self.REG_CAL_WEIGHT, 1)
        if ok is False:
            logger.warning(
                "Failed to trigger weight calibration",
                extra={"unit_id": self.scale_id},
            )
            return {"success": False, "error": "Failed to write weight calibration command"}

        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout

        while loop.time() < deadline:
            await asyncio.sleep(0.2)
            status = await self.get_calibration_status()
            if status["weight_ok"] is True:
                return {"success": True, "status": status}

        logger.warning(
            "Weight calibration timed out",
            extra={"unit_id": self.scale_id, "timeout_s": timeout},
        )
        return {"success": False, "error": "Weight calibration timeout", "status": await self.get_calibration_status()}
