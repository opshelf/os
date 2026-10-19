from __future__ import annotations

import asyncio
import logging
from typing import Optional, Dict, Any, NamedTuple, Tuple
from hardware.controller import SharedModbusController

logger = logging.getLogger(__name__).getChild("scale")


class WeightReading(NamedTuple):
    """One instantaneous weighing, taken without retries."""

    grams: float
    decimals: int
    stable: bool
    valid: bool
    overload: bool


class Scale:
    REG_WEIGHT_HI = 0
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

    # The motion guard's fast weigh owns this port briefly; a dead scale must
    # answer (or fail) inside this window rather than hold the shared lock while
    # pymodbus re-sends for the controller's default 2 s timeout.
    FAST_READ_TIMEOUT_S = 0.1

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

    async def _read_one(self, addr: int) -> Optional[int]:
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

    async def read_weight(self, averages: int = 1, delay: float = 0.1) -> Optional[float]:
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

    async def read_weight_fast(self) -> WeightReading:
        """Weigh in a single Modbus transaction with no retries.

        Motion monitoring is latency bound: a dropped frame is worth less than
        the extra ~75 ms a four-transaction read costs, and a retry inside the
        controller would hold the port lock for another device timeout. The
        decimal position and status flags come along in the same frame so the
        reading stays honest about its own scale factor, and the caller owns the
        failure handling.
        """
        regs = await self.modbus_controller.read_holding_register_block(
            self.scale_id,
            self.REG_WEIGHT_HI,
            4,
            attempts=1,
            modbus_retries=0,
            modbus_timeout_s=self.FAST_READ_TIMEOUT_S,
        )

        hi, lo, decimals, status_raw = regs
        raw = self._to_int32_signed(hi, lo)
        decimals = int(decimals)
        status = self._interpret_status(status_raw)

        return WeightReading(
            grams=raw * (10 ** (-decimals)),
            decimals=decimals,
            stable=status["stable"] is True,
            valid=status["valid"] is True,
            overload=status["overload"] is True,
        )

    async def read_weight_data(self) -> Dict[str, Any]:
        regs = await self._read_block(self.REG_WEIGHT_HI, 4)

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
        try:
            await self.modbus_controller.write_single_register(self.scale_id, self.REG_TARE, 1)
            logger.debug("Scale tare command sent", extra={"unit_id": self.scale_id})

            return True
        except Exception:
            logger.warning(
                "Scale tare failed",
                extra={"unit_id": self.scale_id},
            )
            return False

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
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        saw_unstable = False
        collected_weights: list[float] = []

        while loop.time() < deadline:
            st = await self.read_status()

            if st["stable"] is False:
                saw_unstable = True
                weight = await self.read_weight()

                if weight is not None and weight > 0:
                    collected_weights.append(weight)

                logger.debug(
                    "Scale detected unstable state",
                    extra={"unit_id": self.scale_id, "weight": weight},
                )

            if st["stable"] is True:
                weight = await self.read_weight()

                if weight is not None and weight > 0:
                    logger.debug(
                        "Scale reached stable reading",
                        extra={"unit_id": self.scale_id, "weight_grams": weight},
                    )

                    return weight

                if saw_unstable is True:
                    logger.debug(
                        "Scale stable at zero after instability",
                        extra={"unit_id": self.scale_id, "weight_grams": weight},
                    )

                    return weight

            await asyncio.sleep(poll_interval)

        if len(collected_weights) >= 3:
            avg_weight = sum(collected_weights) / len(collected_weights)

            logger.warning(
                "Scale stability timeout, using averaged weight",
                extra={
                    "unit_id": self.scale_id,
                    "timeout_s": timeout,
                    "samples": len(collected_weights),
                    "avg_weight_grams": avg_weight,
                },
            )

            return avg_weight

        logger.warning(
            "Scale stability wait timed out",
            extra={
                "unit_id": self.scale_id,
                "timeout_s": timeout,
                "poll_interval_s": poll_interval,
                "saw_unstable": saw_unstable,
                "samples_collected": len(collected_weights),
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
