import asyncio
import time
from abc import ABC, abstractmethod
from typing import Any, Awaitable, Dict, Union

from hardware import SharedModbusController
from hardware.drivers.iclrs import Iclrs
from hardware.drivers.iclrs_register_types import (
    AlarmCode,
    MotionStatus,
)
from hardware.exceptions import (
    MotorConfigError,
    MotorError,
    MotorLimitError,
    MotorTimeoutError,
)
from pydantic import BaseModel, Field, create_model, model_validator

from perpetual_os1.common import Motor as MotorConfig


# --- Clamped value basemodel ---
def create_clamped_value_model(
    name: str,
    min: Union[int, float] = 0,
    max: Union[int, float] = 0xFFFFFFFF,
) -> type[BaseModel]:
    @model_validator(mode="after")
    def clamp_value(self):
        model_field = self.__class__.model_fields["clamped_value"]
        ge = model_field.metadata[0].ge
        le = model_field.metadata[1].le
        if self.value > le:
            self.clamped_value = le
        elif self.value < ge:
            self.clamped_value = ge
        else:
            self.clamped_value = self.value
        return self

    fields: Dict[str, Any] = {
        "value": (int | float),
        "clamped_value": (
            int | float | None,
            Field(ge=min, le=max, default=None),
        ),
        "__validators__": {"clamp_value": clamp_value},
    }
    return create_model(name, **fields)


class MotorAxis(ABC):
    """Abstract base class for motor axis"""

    @abstractmethod
    async def configure(self):
        pass

    @abstractmethod
    async def home(self):
        pass

    @abstractmethod
    async def clear_alarm(self):
        pass

    @abstractmethod
    async def stop(self):
        pass

    @abstractmethod
    async def get_position(self) -> float:
        pass

    @abstractmethod
    async def move_to_position(
        self,
        position_mm: float,
        velocity_mmps: float,
        acceleration_mmps2: float,
    ):
        pass

    @abstractmethod
    async def move_to_position_no_wait(
        self,
        position_mm: float,
        velocity_mmps: float,
        acceleration_mmps2: float,
    ):
        pass

    @abstractmethod
    async def wait_for_move(self):
        pass

    @abstractmethod
    async def idle(self):
        pass

    @abstractmethod
    async def ready(self):
        pass

    @abstractmethod
    async def jog(self):
        pass


class MotorAxisIclRs(MotorAxis):
    """Implementation of the MotorAxis abstract base class using the Icl-Rs driver"""

    def __init__(
        self, controller: SharedModbusController, params: MotorConfig
    ):
        self.motor_id = params.motor_id
        self.mm_per_revolution = params.mpr
        self.limit_mm = params.lim
        self.driver = Iclrs(
            controller=controller,
            config=params.driver_config,
        )
        self.position_limiter = create_clamped_value_model(
            name="LimitedPosition", min=0, max=self.limit_mm
        )
        # --- Timeouts ---
        self.homing_timeout_s = params.homing_timeout_s
        self.move_timeout_s = params.move_timeout_s
        # --- Polling ---
        self.polling_delay_s = params.polling_delay_s
        self.polling_period_s = params.polling_period_s
        # --- Default velocity and acceleration ---
        self.default_velocity_mm_per_s = (
            params.default_velocity_mm_per_s
        )
        self.default_acceleration_mm_per_s2 = (
            params.default_acceleration_mm_per_s2
        )
        self.move_try_agains = params.move_try_agains
        # --- Event handling ---
        self.stop_event = asyncio.Event()
        self.stop_event.clear()
        self.safety_curtain_inactive_event = asyncio.Event()
        self.safety_curtain_inactive_event.set()
        self.pending_motion: Awaitable[None] | None
        # --- Kinematics parameters ---
        # Used to scale motor position units [pulses] to mm.
        # To be set when read from device after configuration.
        self.pulse_per_revolution: int = None
        # Used to translate motor controller coordinate to machine coordinate.
        # To be set when read from device after homing.
        self.zero_offset_pulse: int = None

    # --- Implement abstract base class methods
    async def configure(self) -> None:
        try:
            await self.driver.configure()
            self.pulse_per_revolution = (
                await self.driver.read_pulse_per_revolution()
            )
        except Exception as e:
            raise MotorConfigError(
                f"Failed to configure motor {self._motor_id}: {e}"
            ) from e

    # async def home(self) -> None:
    #     await self.safety_curtain_inactive_event.wait()
    #     await self.driver.write_homing_command()
    #     await self.wait_for_move(timeout_s=self.homing_timeout_s)
    #     # Status checks:
    #     alarm = await self.driver.read_alarm()
    #     if alarm.code == AlarmCode.STALL_TORQUE:
    #         # Home via stall torque achieved, as indicated by the alarm.
    #         # Clear the alarm.
    #         await self.driver.clear_current_alarm()
    #         await self.driver.zero_position()
    #     else:
    #         # Stopped without stall torque, due to some other unintended fault.
    #         raise MotorError(
    #             f"Motor {self.motor_id} faulted during homing"
    #         )

    #     self.zero_offset_pulse = await self.driver.read_position()

    async def home(self) -> None:
        for _ in range(self.move_try_agains):
            await self.safety_curtain_inactive_event.wait()
            await self.driver.write_homing_command()
            await self.wait_for_move(timeout_s=self.move_timeout_s)
            status: MotionStatus = await self.driver.read_status()
            # Check reason for move end.
            if not self.safety_curtain_inactive_event.is_set():
                # Safety curtain activated: wait for it to clear, then try again.
                await self.safety_curtain_inactive_event.wait()
                continue
            elif self.stop_event.is_set():
                # Stop command (not from curtain safety) was made: terminate.
                return
            elif not status.is_running:
                if status.is_fault:
                    # Fault was detected: read alarm, clear alarm, then try again.
                    alarm = await self.driver.read_alarm()
                    if alarm.code == AlarmCode.STALL_TORQUE:
                        # Home via stall torque achieved: terminate.
                        await self.driver.clear_current_alarm()
                        await self.driver.zero_position()
                        self.zero_offset_pulse = (
                            await self.driver.read_position()
                        )
                        return
                    else:
                        # Stopped due to some other unintended fault: clear alarm, the try again.
                        await self.clear_alarm()
                        continue
        raise MotorError(
            f"Motor {self.motor_id} faulted during homing"
        )

    async def clear_alarm(self) -> None:
        await self.driver.clear_alarms()

    async def stop(self) -> None:
        await self.driver.write_stop_command()
        self.stop_event.set()

    async def get_position(self) -> float:
        position_pulses = await self.driver.read_position()
        position_mm = self.pulse_to_mm(position_pulses)
        return position_mm

    async def move_to_position(
        self,
        position_mm: float,
        velocity_mm_per_s: float | None = None,
        acceleration_mm_per_s2: float | None = None,
    ) -> None:
        for _ in range(self.move_try_agains):
            await self.move_to_position_no_wait(
                position_mm, velocity_mm_per_s, acceleration_mm_per_s2
            )
            await self.wait_for_move(timeout_s=self.move_timeout_s)
            status: MotionStatus = await self.driver.read_status()
            # Check reason for move end.
            if not self.safety_curtain_inactive_event.is_set():
                # Safety curtain activated: wait for it to clear, then try again.
                await self.safety_curtain_inactive_event.wait()
                continue
            elif self.stop_event.is_set():
                # Stop command (not from curtain safety) was made: terminate.
                return
            elif not status.is_running:
                if status.is_fault:
                    # Fault was detected: read alarm, clear alarm, then try again.
                    alarm = await self.driver.read_alarm()
                    await self.clear_alarm()
                    continue
                elif status.command_completed:
                    # Move successfully complete: confirm position.
                    current_position_mm = await self.get_position()
                    if abs(current_position_mm - position_mm) < 0.1:
                        # Target position reached: terminate.
                        return
                    else:
                        # Target position not reached: try again.
                        continue

    # async def move_to_position(
    #     self,
    #     position_mm: float,
    #     velocity_mm_per_s: float | None = None,
    #     acceleration_mm_per_s2: float | None = None,
    # ) -> None:
    #     await self.move_to_position_no_wait(
    #         position_mm, velocity_mm_per_s, acceleration_mm_per_s2
    #     )
    #     await self.wait_for_move(timeout_s=self.move_timeout_s)
    #     # Motor is now done running, but need to check if it stopped because of fault
    #     # or because it actually completed the path.
    #     status: MotionStatus = await self.driver.read_status()
    #     if status.is_fault:
    #         alarm = await self.driver.read_alarm()
    #         raise MotorError(
    #             f"Motor {self.motor_id} faulted while moving: {alarm.message}"
    #         )

    async def move_to_position_no_wait(
        self,
        position_mm: float,
        velocity_mm_per_s: float | None = None,
        acceleration_mm_per_s2: float | None = None,
    ) -> None:
        if velocity_mm_per_s is None:
            velocity_mm_per_s = self.default_velocity_mm_per_s
        if acceleration_mm_per_s2 is None:
            acceleration_mm_per_s2 = (
                self.default_acceleration_mm_per_s2
            )
        await self.safety_curtain_inactive_event.wait()
        limited_position_mm = self.position_limiter(
            value=position_mm
        ).clamped_value
        pulses = self.mm_to_pulse(limited_position_mm)
        rpm = self.mm_per_s_to_rpm(velocity_mm_per_s)
        accel = self.mm_per_s2_to_s_per_rpm(acceleration_mm_per_s2)
        await self.driver.write_move_to_position(
            position=pulses, rpm=rpm, accel=accel
        )

    async def wait_for_move(self, timeout_s: int) -> None:
        start_time_s = time.monotonic()
        self.stop_event.clear()
        # Delay here to let values settle.
        await asyncio.sleep(self.polling_delay_s)
        while True:
            await asyncio.sleep(self.polling_period_s)
            # Poll status
            # NOTE: Reading status by "pr control" register doesn't work, status gets
            # stuck on 'COMMAND_COMPLETE_WAIT_FOR_POSITIONING' and doesn't change to
            # 'PATH_COMPLETE' even when position is reached.
            # Use 'motion status' instead.
            status: MotionStatus = await self.driver.read_status()
            if not status.is_running:
                break
            # Timeout
            elapsed_s = time.monotonic() - start_time_s
            if elapsed_s > timeout_s:
                raise MotorTimeoutError(
                    f"Motor {self.motor_id} wait for idle timed out"
                )
            if not self.safety_curtain_inactive_event.is_set():
                # Curtain safety is active
                await self.stop()
            # Stop
            if self.stop_event.is_set():
                break

    async def idle(self):
        # Not needed, do nothing.
        pass

    async def ready(self):
        # Not needed, do nothing.
        pass

    async def jog(self, distance_mm: float) -> None:
        current_position_mm = await self.get_position()
        target_position_mm = current_position_mm + distance_mm
        await self.move_to_position(target_position_mm)

    async def activate_safety_curtain(
        self, is_activate: bool
    ) -> None:
        if is_activate:
            # clear() to prevent movements
            self.safety_curtain_inactive_event.clear()
        else:
            # set() to allow movements
            self.safety_curtain_inactive_event.set()

    # --- Helper functions ---
    def pulse_to_mm(self, position_pulse: int) -> float:
        return (
            float(position_pulse - self.zero_offset_pulse)
            / self.pulse_per_revolution
            * self.mm_per_revolution
        )

    def mm_to_pulse(self, position_mm: int) -> int:
        return (
            int(
                position_mm
                * self.pulse_per_revolution
                / self.mm_per_revolution
            )
            + self.zero_offset_pulse
        )

    def mm_per_s_to_rpm(self, mm_per_s: float) -> int:
        return int(mm_per_s * 60 / self.mm_per_revolution)

    def mm_per_s2_to_s_per_rpm(self, mm_per_s2: float) -> int:
        return int(60 * self.mm_per_revolution * (1 / mm_per_s2))


"""
To-do:
1. Add logging
2. Add more specific exceptions and messages

Questions:
1. What to do when position exceeds configured limit? Currently it clamps the position 
value if out-of-limit, then goes to clamped position. I recommend instead for it to
throw an error and not move.   
2. What to do post curtain safety? 
"""
