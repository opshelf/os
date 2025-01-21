from can import BusABC, BufferedReader
from hardware_common.stepper_motor import (
    AbstractStepperMotor,
    StepperMotorConfig,
    Rotation,
    StepperMotorStatus,
)
from perpetual_trinamic.tmcm12xx import TMCM12xx
from perpetual_trinamic.tmcl import StatusFlags, TrinamicErrorFlags
import time
from typing import Optional
from enum import Enum


class UserVariables(int, Enum):
    HOMED = 0x00
    HARD_STOPPED = 0x01


class HomedStatus(int, Enum):
    NOT_HOMED = 0x00
    HOMED = 0x01


class HardStopStatus(int, Enum):
    NOT_HARD_STOPPED = 0x00
    HARD_STOPPED = 0x01


class TrinamicStepper(TMCM12xx):
    def __init__(
        self,
        connection: BusABC,
        configuration: StepperMotorConfig,
        reader: Optional[BufferedReader] = None,
        module_id: int = 1,
        motor_id: int = 0,
        max_current: int = 3000,
    ) -> None:
        TMCM12xx.__init__(self, connection, reader, module_id, motor_id, max_current)
        self._connection = connection
        self._reader = reader
        self._module_id = module_id
        self._motor_id = motor_id
        self._configuration = configuration
        self._status = StepperMotorStatus.GOOD
        self._target_position = 0.0
        self._actual_position = 0.0
        self._max_deviation = 500
        self._homing_deviation = 100
        self.initialize()

    def home_if_errored(self) -> None:
        if (
            self.get_home_status() == HomedStatus.HOMED
            and self.get_hard_stop_status == HardStopStatus.NOT_HARD_STOPPED
        ):
            pass
        else:
            self.home()

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

    def safety_off(self) -> None:
        if self._configuration.external_encoder:
            self.set_external_encoder_max_deviation(0)
            self._homing_deviation = 0
        else:
            self.set_max_encoder_deviation(0)
            self._homing_deviation = 0

    def safety_on(self) -> None:
        if self._configuration.external_encoder:
            self.set_external_encoder_max_deviation(self._max_deviation)
        else:
            self.set_max_encoder_deviation(self._max_deviation)

    def homing_safety_on(self) -> None:
        if self._configuration.external_encoder:
            self.set_external_encoder_max_deviation(self._homing_deviation)
        else:
            self.set_max_encoder_deviation(self._homing_deviation)

    def encoder_position(self) -> int:
        if self._configuration.external_encoder:
            return self.get_external_encoder_postion()
        else:
            return self.get_encoder_position()

    def _set_encoder_position(self, position: int) -> None:
        if self._configuration.external_encoder:
            self.set_external_encoder_position(position)
        else:
            self.set_encoder_position(position)

    def initialize(self) -> None:
        self.set_max_current(self._configuration.work_current)
        if self._configuration.brake:
            self.set_standby_current(0)
        else:
            self.set_standby_current(self._configuration.holding_current)
        self.set_microsteps(self.MicroStepResolution.MicrostepResolution16Microsteps)
        self.safety_on()
        self.set_relative_position_mode()
        self._target_position = self.get_position()
        position = self.encoder_position()
        self.set_actual_position(position)
        self.set_target_position(position)
        # self.set_speed(self._configuration.velocity)
        # self.set_acceleration(self._configuration.velocity)
        self.clear_error_flags()
        self._status = StepperMotorStatus.GOOD

    def rotate(self, value: int) -> None:
        if self._configuration.invert_coordinates:
            if value > 0:
                self.rotate_left(self._configuration.convert_mms_ps(value))
            else:
                self.rotate_right(abs(self._configuration.convert_mms_ps(value)))
        else:
            if value > 0:
                self.rotate_right(self._configuration.convert_mms_ps(value))
            else:
                self.rotate_left(abs(self._configuration.convert_mms_ps(value)))

    def get_home_status(self) -> HomedStatus:
        status = self.get_user_variable(UserVariables.HOMED.value)
        if status == HomedStatus.HOMED:
            return HomedStatus.HOMED
        else:
            return HomedStatus.NOT_HOMED

    def set_home_status(self, status: HomedStatus) -> None:
        self.set_user_variable(UserVariables.HOMED.value, status.value)

    def set_hard_stop_status(self, status: HardStopStatus) -> None:
        self.set_user_variable(UserVariables.HARD_STOPPED.value, status.value)

    def get_hard_stop_status(self) -> HardStopStatus:
        status = self.get_user_variable(UserVariables.HARD_STOPPED.value)
        if status.value == HardStopStatus.HARD_STOPPED:
            return HardStopStatus.HARD_STOPPED
        else:
            return HardStopStatus.NOT_HARD_STOPPED

    def home(self) -> None:
        self.clear_error_flags()
        self.get_status_flags()
        self.set_actual_position(0)
        self.set_target_position(0)
        self._set_encoder_position(0)
        self.set_max_current(self._configuration.homing_current)
        self.set_microsteps(self.MicroStepResolution.MicrostepResolution16Microsteps)
        self.homing_safety_on()
        if self._configuration.home_direction == Rotation.CW:
            if self._configuration.homing_speed:
                # self.set_acceleration(
                #    self._configuration.homing_speed
                # )
                self.rotate_right(
                    self._configuration.convert_mms_ps(self._configuration.homing_speed)
                )
            else:
                self.rotate_right(3000)
        else:
            if self._configuration.homing_speed:
                # self.set_acceleration(
                #    self._configuration.homing_speed
                # )
                self.rotate_left(
                    self._configuration.convert_mms_ps(self._configuration.homing_speed)
                )
            else:
                self.rotate_left(3000)
        t0 = time.time()
        while True:
            if self.get_error_flags():
                break
            else:
                if time.time() - t0 > 20:
                    raise Exception(f"Homing Failed {self._module_id}")
        while self.get_error_flags():
            pass
        self.get_status_flags()
        self.initialize()
        self.set_actual_position(0)
        self.set_target_position(0)
        self._set_encoder_position(0)
        self._target_position = 0
        self._actual_position = 0
        self.set_home_status(HomedStatus.HOMED)
        self.set_hard_stop_status(HardStopStatus.NOT_HARD_STOPPED)

    def check_fault_flags(self) -> bool:
        error_flags = TrinamicErrorFlags(value=self.get_error_flags())
        status_flags = StatusFlags(self.get_status_flags())
        return error_flags.is_fault() or status_flags.is_fault()

    def clear_error_flags(self) -> None:
        while self.get_error_flags():
            pass
        self._status = StepperMotorStatus.GOOD

    def clear_status_flags(self) -> None:
        while True:
            status_flags = StatusFlags(self.get_status_flags())
            if status_flags.is_fault():
                pass
            else:
                break
        self._status = StepperMotorStatus.GOOD

    def get_position(self) -> float:
        position = self.encoder_position()
        self._actual_position = self._configuration.convert_to_revolutions(position)
        return self._actual_position

    def move_revolutions(self, position: float) -> None:
        if self._status == StepperMotorStatus.GOOD:
            position = self._configuration.convert_revolutions_to_steps(position)
            self._target_position = position
            self.move_absolute(position)
        else:
            raise Exception("Status Bad")

    def move_relative_revolutions(self, position: float) -> None:
        if self._status == StepperMotorStatus.GOOD:
            self._target_position = self._target_position + position
            position = self._configuration.convert_revolutions_to_steps(position)
            self.move_relative(position)

    def wait_for_move(self, timeout: float = 60.0) -> None:
        t0 = time.time()
        while not self.get_position_reached():
            time.sleep(0.05)
            if self.get_error_flags():
                self.set_hard_stop_status(HardStopStatus.HARD_STOPPED)
                raise Exception(f"Failed to move hit hard stop {self._module_id}")
            if time.time() - t0 > timeout:
                self._status = StepperMotorStatus.ERROR
                self.set_hard_stop_status(HardStopStatus.HARD_STOPPED)
                raise Exception("Failed to move in time")

    def moving(self) -> bool:
        if self.get_position_reached():
            return True
        if self.get_error_flags():
            return True
        return False

    def stop_motor(self) -> None:
        self.stop()

    def get_status(self) -> StepperMotorStatus:
        status = self.get_error_flags()
        if status:
            self._status = StepperMotorStatus.ERROR
        return self._status

    # def set_speed(self, mms_ps: float) -> None:
    #    speed = self._configuration.convert_mms_ps(mms_ps)
    #    self.set_max_velocity(speed)

    # def set_acceleration(self, mmss: float) -> None:
    #    accel = self._configuration.convert_mms_ps(mmss)
    #    self.set_max_acceleration(accel)


class TrinamicAxis(AbstractStepperMotor, TMCM12xx):
    def __init__(
        self,
        connection: BusABC,
        configuration: StepperMotorConfig,
        reader: Optional[BufferedReader] = None,
        module_id: int = 1,
        motor_id: int = 0,
        max_current: int = 3000,
    ) -> None:
        TMCM12xx.__init__(self, connection, reader, module_id, motor_id, max_current)
        self._connection = connection
        self._reader = reader
        # def __init__(
        #    self,
        #    connection: BusABC,
        #    module_id: int,
        #    motor_id: int,
        #    configuration: StepperMotorConfig,
        #    max_current: int = 3000,
        # ) -> None:
        #    TMCM12xx.__init__(self, connection, module_id, motor_id, max_current)
        #    self._connection = connection
        self._module_id = module_id
        self._motor_id = motor_id
        self._configuration = configuration
        self._status = StepperMotorStatus.GOOD
        self._target_position = 0.0
        self._actual_position = 0.0
        self._max_deviation = 500
        self._homing_deviation = 100
        self.initialize()

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

    def safety_off(self) -> None:
        if self._configuration.external_encoder:
            self.set_external_encoder_max_deviation(0)
        else:
            self.set_max_encoder_deviation(0)

    def safety_on(self) -> None:
        if self._configuration.external_encoder:
            self.set_external_encoder_max_deviation(self._max_deviation)
        else:
            self.set_max_encoder_deviation(self._max_deviation)

    def homing_safety_on(self) -> None:
        if self._configuration.external_encoder:
            self.set_external_encoder_max_deviation(self._homing_deviation)
        else:
            self.set_max_encoder_deviation(self._homing_deviation)

    def encoder_position(self) -> int:
        if self._configuration.external_encoder:
            return self.get_external_encoder_postion()
        else:
            return self.get_encoder_position()

    def _set_encoder_position(self, position: int) -> None:
        if self._configuration.external_encoder:
            self.set_external_encoder_position(position)
        else:
            self.set_encoder_position(position)

    def reset_location(self) -> None:
        self.set_actual_position(self.get_encoder_position())

    def initialize(self) -> None:
        self.set_max_current(self._configuration.work_current)
        self.set_standby_current(self._configuration.holding_current)
        self.set_microsteps(self.MicroStepResolution.MicrostepResolution16Microsteps)
        self.safety_on()
        self.set_relative_position_mode()
        self._target_position = self.get_position()
        position = self.encoder_position()
        self.set_actual_position(position)
        self.set_target_position(position)
        self.set_speed(self._configuration.velocity)
        self.set_acceleration(self._configuration.acceleration)
        self.clear_error_flags()
        self._status = StepperMotorStatus.GOOD

    def rotate(self, value: int) -> None:
        if self._configuration.invert_coordinates:
            if value > 0:
                self.rotate_left(abs(self._configuration.convert_mms_ps(value)))
            else:
                self.rotate_right(self._configuration.convert_mms_ps(value))
        else:
            if value > 0:
                self.rotate_right(self._configuration.convert_mms_ps(value))
            else:
                self.rotate_left(abs(self._configuration.convert_mms_ps(value)))

    def home(self) -> None:
        self.set_standby_current(self._configuration.holding_current)
        self.clear_error_flags()
        self.get_status_flags()
        self.set_actual_position(0)
        self.set_target_position(0)
        self._set_encoder_position(0)
        self.set_max_current(self._configuration.homing_current)
        self.set_microsteps(self.MicroStepResolution.MicrostepResolution16Microsteps)
        self.homing_safety_on()
        if self._configuration.home_direction == Rotation.CW:
            if self._configuration.homing_speed:
                self.set_acceleration(self._configuration.homing_speed)
                self.rotate_right(
                    self._configuration.convert_mms_ps(self._configuration.homing_speed)
                )
            else:
                self.rotate_right(3000)
        else:
            if self._configuration.homing_speed:
                self.set_acceleration(self._configuration.homing_speed)
                self.rotate_left(
                    self._configuration.convert_mms_ps(self._configuration.homing_speed)
                )
            else:
                self.rotate_left(3000)
        t0 = time.time()
        while True:
            if self.get_error_flags():
                break
            else:
                if time.time() - t0 > 30:
                    raise Exception(f"Homing Failed {self._module_id}")
        while self.get_error_flags():
            pass
        self.get_status_flags()
        self.initialize()
        self.set_actual_position(0)
        self.set_target_position(0)
        self._set_encoder_position(0)
        self._target_position = 0
        self._actual_position = 0
        self.set_home_status(HomedStatus.HOMED)
        self.set_hard_stop_status(HardStopStatus.NOT_HARD_STOPPED)

    def check_fault_flags(self) -> bool:
        error_flags = TrinamicErrorFlags(value=self.get_error_flags())
        status_flags = StatusFlags(self.get_status_flags())
        return error_flags.is_fault() or status_flags.is_fault()

    def clear_error_flags(self) -> None:
        while self.get_error_flags():
            pass
        self._status = StepperMotorStatus.GOOD

    def clear_status_flags(self) -> None:
        while True:
            status_flags = StatusFlags(self.get_status_flags())
            if status_flags.is_fault():
                pass
            else:
                break
        self._status = StepperMotorStatus.GOOD

    def get_position(self) -> float:
        position = self.encoder_position()
        self._actual_position = self._configuration.convert_encoder_position(position)
        return self._actual_position

    def move_to_position(self, position: float) -> None:
        if self._status == StepperMotorStatus.GOOD:
            position = self._configuration.convert_position_to_pulses(position)
            self._target_position = position
            self.move_absolute(position)
        else:
            raise Exception("Status Bad")

    def move_to_relative_position(self, position: float) -> None:
        if self._status == StepperMotorStatus.GOOD:
            if self._target_position + position >= 0:
                self._target_position = self._target_position + position
                position = self._configuration.convert_position_to_pulses(position)
                self.move_relative(position)
            else:
                self._target_position = 0
                self.move_to_position(0)

    def wait_for_move(self, timeout: float = 60.0) -> None:
        t0 = time.time()
        while not self.get_position_reached():
            time.sleep(0.1)
            if self.get_error_flags():
                raise Exception(f"Failed to move hit hard stop {self._module_id}")
            if time.time() - t0 > timeout:
                self._status = StepperMotorStatus.ERROR
                raise Exception(f"Failed to move in time {self._module_id}")

    def poll_position_reached(self) -> bool:
        if self.get_position_reached():
            return True
        else:
            if self.get_error_flags():
                raise Exception(f"Failed to move hit hard stop {self._module_id}")
            return False

    def moving(self) -> bool:
        if self.get_position_reached():
            return True
        if self.get_error_flags():
            return True
        return False

    def stop_motor(self) -> None:
        self.stop()

    def get_status(self) -> StepperMotorStatus:
        status = self.get_error_flags()
        if status:
            self._status = StepperMotorStatus.ERROR
        return self._status

    def set_speed(self, mms_ps: float) -> None:
        speed = self._configuration.convert_mms_ps(mms_ps)
        self.set_max_velocity(speed)

    def set_acceleration(self, mmss: float) -> None:
        accel = self._configuration.convert_mms_ps(mmss)
        self.set_max_acceleration(accel)

    @property
    def configuration(self) -> StepperMotorConfig:
        return self._configuration

    @configuration.setter
    def configuration(self, configuration: StepperMotorConfig) -> None:
        self._configuration = configuration

    def move_to_position_with_brake(self, position: float, timeout: float = 60.0) -> None:
        if self._status != StepperMotorStatus.GOOD:
            raise Exception("Status Bad")
        self.reset_location()
        self.init_brake_move()
        self.move_to_position(position)
        self.wait_for_brake_move()
        self.end_brake_move()

    def move_to_relative_position_with_brake(self, position: float, timeout: float = 60.0) -> None:
        if self._status != StepperMotorStatus.GOOD:
            raise Exception("Status Bad")
        self.init_brake_move()
        self.move_to_relative_position(position)
        self.wait_for_brake_move(timeout)
        self.end_brake_move()

    def wait_for_brake_move(self, timeout: float = 60.0) -> None:
        t0 = time.time()
        while not self.get_position_reached():
            if self.get_error_flags():
                self.end_brake_move()
                raise Exception(f"Failed to move hit hard stop {self._module_id}")
            if time.time() - t0 > timeout:
                self._status = StepperMotorStatus.ERROR
                self.end_brake_move()
                raise Exception(f"Failed to move in time {self._module_id}")
            time.sleep(0.01)

    def home_with_brake(self) -> None:
        self.init_brake_move()
        self.home()
        self.end_brake_move()

    def init_brake_move(self) -> None:
        self.set_standby_current(self._configuration.holding_current)
        time.sleep(0.1)
        self.set_io(0, 1, 2)
        time.sleep(0.2)

    def end_brake_move(self) -> None:
        self.set_io(0, 0, 2)
        time.sleep(0.2)
        self.set_standby_current(0)

    def get_home_status(self) -> HomedStatus:
        status = self.get_user_variable(UserVariables.HOMED.value)
        if status == HomedStatus.HOMED:
            return HomedStatus.HOMED
        else:
            return HomedStatus.NOT_HOMED

    def set_home_status(self, status: HomedStatus) -> None:
        self.set_user_variable(UserVariables.HOMED.value, status.value)

    def set_hard_stop_status(self, status: HardStopStatus) -> None:
        self.set_user_variable(UserVariables.HARD_STOPPED.value, status.value)

    def get_hard_stop_status(self) -> HardStopStatus:
        status = self.get_user_variable(UserVariables.HARD_STOPPED.value)
        if status.value == HardStopStatus.HARD_STOPPED:
            return HardStopStatus.HARD_STOPPED
        else:
            return HardStopStatus.NOT_HARD_STOPPED
