from hardware_common.stepper_motor import (
    StepperMotorConfig,
    Rotation,
    StepperMotorStatus,
)
from perpetual_trinamic.aio.tmcm12xx import TMCM12xx
from perpetual_trinamic.tmcl import StatusFlags, TrinamicErrorFlags
from perpetual_trinamic.aio.common import UserVariables, HomedStatus, HardStopStatus
from perpetual_can import CANBusHandler
from typing import Optional
import asyncio
import time
from pydantic import BaseModel


class AxisStatus(BaseModel):
    status_flags: StatusFlags
    error_flags: TrinamicErrorFlags
    position: float
    homed: HomedStatus
    hard_stopped: HardStopStatus


class TrinamicAxis(TMCM12xx):
    def __init__(
        self,
        bus: CANBusHandler,
        configuration: StepperMotorConfig,
        module_id: int = 1,
        motor_id: int = 0,
        max_current: int = 3000,
    ) -> None:
        TMCM12xx.__init__(self, bus, module_id, motor_id, max_current)
        self._bus = bus
        self._module_id = module_id
        self._motor_id = motor_id
        self._configuration = configuration
        self._status = StepperMotorStatus.GOOD
        self._target_position = 0.0
        self._actual_position = 0.0

    async def safety_off(self) -> None:
        if self._configuration.external_encoder:
            await self.set_external_encoder_max_deviation(0)
        else:
            await self.set_max_encoder_deviation(0)

    async def safety_on(self) -> None:
        if self._configuration.external_encoder:
            await self.set_external_encoder_max_deviation(self._configuration.max_encoder_deviation)
        else:
            await self.set_max_encoder_deviation(self._configuration.max_encoder_deviation)

    async def homing_safety_on(self) -> None:
        if self._configuration.external_encoder:
            await self.set_external_encoder_max_deviation(
                self._configuration.max_encoder_deviation_homing
            )
        else:
            await self.set_max_encoder_deviation(self._configuration.max_encoder_deviation_homing)

    async def encoder_position(self) -> int:
        if self._configuration.external_encoder:
            return await self.get_external_encoder_postion()
        else:
            return await self.get_encoder_position()

    async def _set_encoder_position(self, position: int) -> None:
        if self._configuration.external_encoder:
            await self.set_external_encoder_position(position)
        else:
            await self.set_encoder_position(position)

    async def reset_location(self) -> None:
        encoder_position = await self.get_encoder_position() 
        await self.set_actual_position(encoder_position)

    async def sync_position(self) -> None:
        position = await self.get_encoder_position()
        await self.set_actual_position(position)
        await self.set_target_position(position)
        self._target_position = position

    async def initialize(self) -> None:
        await self.set_max_current(self._configuration.work_current)
        if self._configuration.brake:
            await self.set_standby_current(0)
        else:
            await self.set_standby_current(self._configuration.holding_current)
        await self.set_microsteps(self.MicroStepResolution.MicrostepResolution16Microsteps)
        if self._configuration.safety:
            await self.safety_on()
        else:
            await self.safety_off()
        await self.set_relative_position_mode()
        await self.sync_position()
        await self.set_speed(self._configuration.velocity)
        await self.set_acceleration(self._configuration.acceleration)
        await self.clear_error_flags()
        # await self.clear_status_flags()
        self._status = StepperMotorStatus.GOOD

    async def rotate(self, value: int) -> None:
        if self._configuration.invert_coordinates:
            if value > 0:
                await self.rotate_left(abs(self._configuration.convert_mms_ps(value)))
            else:
                await self.rotate_right(self._configuration.convert_mms_ps(value))
        else:
            if value > 0:
                await self.rotate_right(self._configuration.convert_mms_ps(value))
            else:
                await self.rotate_left(abs(self._configuration.convert_mms_ps(value)))

    async def home(self) -> None:
        if self.configuration.brake:
            await self.init_brake_move()
        await self.set_standby_current(self._configuration.holding_current)
        await self.clear_error_flags()
        await self.get_status_flags()
        await self.set_actual_position(0)
        await self.set_target_position(0)
        await self._set_encoder_position(0)
        await self.set_max_current(self._configuration.homing_current)
        await self.set_microsteps(self.MicroStepResolution.MicrostepResolution16Microsteps)
        await self.homing_safety_on()
        if self._configuration.home_direction == Rotation.CW:
            if self._configuration.homing_speed:
                await self.set_acceleration(self._configuration.homing_speed)
                await self.rotate_right(
                    self._configuration.convert_mms_ps(self._configuration.homing_speed)
                )
            else:
                await self.rotate_right(3000)
        else:
            if self._configuration.homing_speed:
                await self.set_acceleration(self._configuration.homing_speed)
                await self.rotate_left(
                    self._configuration.convert_mms_ps(self._configuration.homing_speed)
                )
            else:
                await self.rotate_left(3000)
        t0 = time.time()
        while True:
            if await self.get_error_flags():
                break
            else:
                if time.time() - t0 > 30:
                    await self.stop()
                    raise Exception(f"Homing Failed {self._module_id}")
        while await self.get_error_flags():
            pass
        await self.get_status_flags()
        await self.initialize()
        await self.set_actual_position(0)
        await self.set_target_position(0)
        await self._set_encoder_position(0)
        self._target_position = 0
        self._actual_position = 0
        await self.set_home_status(HomedStatus.HOMED)
        await self.set_hard_stop_status(HardStopStatus.NOT_HARD_STOPPED)
        if self.configuration.brake:
            await self.end_brake_move()

    async def check_fault_flags(self) -> bool:
        error_flags = TrinamicErrorFlags(await self.get_error_flags())
        status_flags = StatusFlags(await self.get_status_flags())
        return error_flags.is_fault() or status_flags.is_fault()

    async def clear_error_flags(self) -> None:
        while await self.get_error_flags():
            pass
        self._status = StepperMotorStatus.GOOD

    async def clear_status_flags(self) -> None:
        while True:
            status_flags = StatusFlags(await self.get_status_flags())
            if status_flags.is_fault():
                pass
            else:
                break
        self._status = StepperMotorStatus.GOOD

    async def get_position(self) -> float:
        position = await self.encoder_position()
        self._actual_position = self._configuration.convert_encoder_position(position)
        return self._actual_position

    async def move_to_position(
        self, position: float, override_brake: Optional[bool] = False
    ) -> None:
        if self._status != StepperMotorStatus.GOOD:
            print("Status Bad")
            raise Exception("Status Bad")
        if self.configuration.brake and not override_brake:
            await self.init_brake_move()
            position = self._configuration.convert_position_to_pulses(position)
            print(f"Target Postion: {position}")
            self._target_position = position
            await self.move_absolute(position)
            print(f"Sent Absolute Move")
            await self.wait_for_move()
            await self.end_brake_move()
        else:
            position = self._configuration.convert_position_to_pulses(position)
            print(f"Target Postion: {position}")
            self._target_position = position
            await self.move_absolute(position)
            print(f"Sent Absolute Move")

    async def move_to_relative_position(
        self, position: float, override_brake: Optional[bool] = False
    ) -> None:
        if self._status != StepperMotorStatus.GOOD:
            raise Exception("Status Bad")
        if self.configuration.brake and not override_brake:
            if self._target_position + position >= 0:
                await self.reset_location()
                await self.init_brake_move()
                self._target_position = self._target_position + position
                position = self._configuration.convert_position_to_pulses(position)
                await self.move_relative(position)
                await self.wait_for_move()
                await self.end_brake_move()
            else:
                self._target_position = 0
                await self.move_to_position(0)
        else:
            if self._target_position + position >= 0:
                self._target_position = self._target_position + position
                position = self._configuration.convert_position_to_pulses(position)
                await self.move_relative(position)
            else:
                self._target_position = 0
                await self.move_to_position(0)

    async def wait_for_move(self, timeout: float = 60.0) -> None:
        if self.configuration.brake:
            t0 = time.time()
            while not await self.get_position_reached():
                error_flags = await self.get_error_flags()
                if error_flags:
                    print(f"{self._module_id}", TrinamicErrorFlags(value=error_flags))
                    await self.end_brake_move()
                    raise Exception(f"Failed to move hit hard stop {self._module_id}")
                if time.time() - t0 > timeout:
                    self._status = StepperMotorStatus.ERROR
                    await self.end_brake_move()
                    raise Exception(f"Failed to move in time {self._module_id}")
                await asyncio.sleep(0.1)
        else:
            t0 = time.time()
            while not await self.get_position_reached():
                error_flags = await self.get_error_flags()
                if error_flags:
                    print(f"{self._module_id}", TrinamicErrorFlags(value=error_flags))
                    raise Exception(f"Failed to move hit hard stop {self._module_id}")
                if time.time() - t0 > timeout:
                    self._status = StepperMotorStatus.ERROR
                    raise Exception(f"Failed to move in time {self._module_id}")
                await asyncio.sleep(0.1)

    async def poll_position_reached(self) -> bool:
        if await self.get_position_reached():
            return True
        else:
            if await self.get_error_flags():
                raise Exception(f"Failed to move hit hard stop {self._module_id}")
            return False

    async def moving(self) -> bool:
        if await self.get_position_reached():
            return True
        if await self.get_error_flags():
            return True
        return False

    async def stop_motor(self) -> None:
        await self.stop()

    async def get_status(self) -> StepperMotorStatus:
        status = await self.get_error_flags()
        if status:
            self._status = StepperMotorStatus.ERROR
        return self._status

    async def set_speed(self, mms_ps: float) -> None:
        speed = self._configuration.convert_mms_ps(mms_ps)
        await self.set_max_velocity(speed)

    async def set_acceleration(self, mmss: float) -> None:
        accel = self._configuration.convert_mms_ps(mmss)
        await self.set_max_acceleration(accel)

    @property
    def configuration(self) -> StepperMotorConfig:
        return self._configuration

    @configuration.setter
    def configuration(self, configuration: StepperMotorConfig) -> None:
        self._configuration = configuration

    async def init_brake_move(self) -> None:
        await self.set_standby_current(self._configuration.holding_current)
        await asyncio.sleep(0.2)
        await self.set_io(0, 1, 2)
        await asyncio.sleep(0.2)

    async def end_brake_move(self) -> None:
        await self.set_io(0, 0, 2)
        await asyncio.sleep(0.1)
        await self.set_standby_current(0)
        await asyncio.sleep(0.1)

    async def get_home_status(self) -> HomedStatus:
        status = await self.get_user_variable(UserVariables.HOMED.value)
        if status == HomedStatus.HOMED:
            return HomedStatus.HOMED
        else:
            return HomedStatus.NOT_HOMED

    async def set_home_status(self, status: HomedStatus) -> None:
        await self.set_user_variable(UserVariables.HOMED.value, status.value)

    async def set_hard_stop_status(self, status: HardStopStatus) -> None:
        await self.set_user_variable(UserVariables.HARD_STOPPED.value, status.value)

    async def get_hard_stop_status(self) -> HardStopStatus:
        status = await self.get_user_variable(UserVariables.HARD_STOPPED.value)
        if status.value == HardStopStatus.HARD_STOPPED:
            return HardStopStatus.HARD_STOPPED
        else:
            return HardStopStatus.NOT_HARD_STOPPED

    async def status(self) -> AxisStatus:
        home_status = await self.get_home_status()
        hard_stop_status = await self.get_hard_stop_status()
        position = await self.get_position()
        status_flags = await self.get_status_flags()
        error_flags = await self.get_error_flags()
        return AxisStatus(
            status_flags=StatusFlags(status_flags),
            error_flags=TrinamicErrorFlags(value=error_flags),
            position=position,
            homed=home_status,
            hard_stopped=hard_stop_status,
        )
