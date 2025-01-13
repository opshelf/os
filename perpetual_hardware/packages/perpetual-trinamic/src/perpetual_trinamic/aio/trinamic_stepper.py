from can.notifier import Notifier
from can import BusABC
from hardware_common.stepper_motor import (
    AbstractStepperMotor,
    StepperMotorConfig,
    Rotation,
    StepperMotorStatus,
)
from perpetual_trinamic.aio.tmcm12xx import TMCM12xx
from perpetual_trinamic.tmcl import StatusFlags, TrinamicErrorFlags
from perpetual_can import CANBusHandler
import time
from perpetual_trinamic.aio.common import UserVariables, HomedStatus, HardStopStatus


class TrinamicStepper(TMCM12xx):
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
        self._max_deviation = 500
        self._homing_deviation = 100

    async def home_if_errored(self) -> None:
        if (
            await self.get_home_status() == HomedStatus.HOMED
            and self.get_hard_stop_status == HardStopStatus.NOT_HARD_STOPPED
        ):
            pass
        else:
            await self.home()

    @property
    def max_deviation(self) -> int:
        return self._max_deviation

    @max_deviation.setter
    def max_deviation(self, value: int) -> None:
        self._max_deviation = value

    @property
    def homing_deviation(self) -> int:
        return self._homing_deviation

    @homing_deviation.setter
    def homing_deviation(self, value: int) -> None:
        self._homing_deviation = value

    async def safety_off(self) -> None:
        if self._configuration.external_encoder:
            await self.set_external_encoder_max_deviation(0)
            self._homing_deviation = 0
        else:
            await self.set_max_encoder_deviation(0)
            self._homing_deviation = 0

    async def safety_on(self) -> None:
        if self._configuration.external_encoder:
            await self.set_external_encoder_max_deviation(self._max_deviation)
        else:
            await self.set_max_encoder_deviation(self._max_deviation)

    async def homing_safety_on(self) -> None:
        if self._configuration.external_encoder:
            await self.set_external_encoder_max_deviation(self._homing_deviation)
        else:
            await self.set_max_encoder_deviation(self._homing_deviation)

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

    async def initialize(self) -> None:
        await self.set_max_current(self._configuration.work_current)
        await self.set_standby_current(self._configuration.holding_current)
        await self.set_microsteps(self.MicroStepResolution.MicrostepResolution16Microsteps)
        if self._configuration.safety:
            await self.safety_on()
        else:
            await self.safety_off()
        await self.set_relative_position_mode()
        self._target_position = await self.get_position()
        position = await self.encoder_position()
        await self.set_actual_position(position)
        await self.set_target_position(position)
        await self.clear_error_flags()
        #await self.clear_status_flags()
        self._status = StepperMotorStatus.GOOD

    async def rotate(self, value: int) -> None:
        if self._configuration.invert_coordinates:
            if value > 0:
                await self.rotate_left(self._configuration.convert_mms_ps(value))
            else:
                await self.rotate_right(abs(self._configuration.convert_mms_ps(value)))
        else:
            if value > 0:
                await self.rotate_right(self._configuration.convert_mms_ps(value))
            else:
                await self.rotate_left(abs(self._configuration.convert_mms_ps(value)))

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

    async def home(self) -> None:
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
                # self.set_acceleration(
                #    self._configuration.homing_speed
                # )
                await self.rotate_right(
                    self._configuration.convert_mms_ps(self._configuration.homing_speed)
                )
            else:
                await self.rotate_right(3000)
        else:
            if self._configuration.homing_speed:
                # self.set_acceleration(
                #    self._configuration.homing_speed
                # )
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
                if time.time() - t0 > 20:
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
        self._actual_position = self._configuration.convert_to_revolutions(position)
        return self._actual_position

    async def move_revolutions(self, position: float) -> None:
        if self._status == StepperMotorStatus.GOOD:
            position = self._configuration.convert_revolutions_to_steps(position)
            self._target_position = position
            await self.move_absolute(position)
        else:
            raise Exception("Status Bad")

    async def move_relative_revolutions(self, position: float) -> None:
        if self._status == StepperMotorStatus.GOOD:
            self._target_position = self._target_position + position
            position = self._configuration.convert_revolutions_to_steps(position)
            await self.move_relative(position)

    @property
    def configuration(self) -> StepperMotorConfig:
        return self._configuration

    @configuration.setter
    def configuration(self, config: StepperMotorConfig) -> None:
        self._configuration = config

    async def wait_for_move(self, timeout: float = 60.0) -> None:
        t0 = time.time()
        while not await self.get_position_reached():
            time.sleep(0.05)
            if await self.get_error_flags():
                await self.set_hard_stop_status(HardStopStatus.HARD_STOPPED)
                raise Exception(f"Failed to move hit hard stop {self._module_id}")
            if time.time() - t0 > timeout:
                self._status = StepperMotorStatus.ERROR
                await self.set_hard_stop_status(HardStopStatus.HARD_STOPPED)
                raise Exception("Failed to move in time")

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
