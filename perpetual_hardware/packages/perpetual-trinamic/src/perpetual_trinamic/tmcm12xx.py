from .tmcm import TMCM

# from .tmcl import TMCLStatus, TMCLCommand, TrinamicErrorFlags
from can import BusABC


class TMCM12xx(TMCM):
    def __init__(
        self,
        connection: BusABC,
        module_id: int = 1,
        motor_id: int = 0,
        max_current: int = 3000,
    ):
        super().__init__(connection, module_id, motor_id)
        self._module_id = module_id
        self._motor_id = motor_id
        self._max_current = max_current

    @property
    def max_current(self) -> int:
        return self._max_current

    @max_current.setter
    def max_current(self, value: int) -> None:
        self._max_current = value

    def get_actual_position(self) -> int:
        reply = self.get_axis_parameter(self.AP.ActualPosition)
        if reply:
            return reply.value
        raise Exception("No Response From Module")

    def get_target_position(self) -> int:
        reply = self.get_axis_parameter(self.AP.TargetPosition)
        if reply:
            return reply.value
        raise Exception("No Response From Module")

    def set_actual_position(self, position: int) -> None:
        self.set_axis_parameter(self.AP.ActualPosition, position)

    def set_target_position(self, position: int) -> None:
        self.set_axis_parameter(self.AP.TargetPosition, position)

    def set_encoder_position(self, position: int) -> None:
        self.set_axis_parameter(self.AP.EncoderPosition, position)

    def set_max_velocity(self, velocity: int) -> None:
        self.set_axis_parameter(self.AP.MaxVelocity, velocity)

    def set_max_acceleration(self, acceleration: int) -> None:
        self.set_axis_parameter(self.AP.MaxAcceleration, acceleration)

    def set_max_current(self, current: int) -> None:
        curr = (current / self._max_current) * 255
        self.set_axis_parameter(self.AP.MaxCurrent, int(curr))

    def set_standby_current(self, current: int) -> None:
        curr = (current / self._max_current) * 255
        self.set_axis_parameter(self.AP.StandbyCurrent, int(curr))

    def get_position_reached(self) -> bool:
        reply = self.get_axis_parameter(self.AP.PositionReachedFlag)
        if reply:
            return bool(reply.value)
        raise Exception("No Response From Module")

    def set_microsteps(self, microsteps: int) -> None:
        self.set_axis_parameter(self.AP.MicrostepResolution, microsteps)

    def get_error_flags(self) -> int:
        reply = self.get_axis_parameter(self.AP.ErrorFlags)
        if reply:
            return reply.value
        raise Exception("No Response From Module")

    def get_status_flags(self) -> int:
        reply = self.get_axis_parameter(self.AP.StatusFlags)
        if reply:
            return reply.value
        raise Exception("No Response From Module")

    def get_encoder_position(self) -> int:
        reply = self.get_axis_parameter(self.AP.EncoderPosition)
        if reply:
            return reply.value
        raise Exception("No Response From Module")

    def set_max_encoder_deviation(self, max_deviation: int) -> None:
        self.set_axis_parameter(self.AP.MaxEncoderDeviation, max_deviation)

    def set_relative_position_mode(self) -> None:
        self.set_axis_parameter(self.AP.RelativePositioningOption, 1)

    def set_external_encoder_resolution(self, resolution: int) -> None:
        self.set_axis_parameter(self.AP.ExternalEncoderResolution, resolution)

    def set_external_encoder_max_deviation(self, max_deviation: int) -> None:
        self.set_axis_parameter(self.AP.ExternalEncoderMaxDeviation, max_deviation)

    def set_external_encoder_position(self, position: int) -> None:
        self.set_axis_parameter(self.AP.ExternalEncoderPosition, position)

    def set_pullup_resistors(self, value: int) -> None:
        self.set_io(0, value)

    def set_outputs(self, value: int) -> None:
        self.set_io(1, value)

    def get_external_encoder_postion(self) -> int:
        reply = self.get_axis_parameter(self.AP.ExternalEncoderPosition)
        if reply:
            return reply.value
        raise Exception("No Response From Module")

    class AP:
        TargetPosition = 0
        ActualPosition = 1
        TargetVelocity = 2
        ActualVelocity = 3
        MaxVelocity = 4
        MaxAcceleration = 5
        MaxCurrent = 6
        StandbyCurrent = 7
        PositionReachedFlag = 8
        HomeSwitch = 9
        RightEndstop = 10
        LeftEndstop = 11
        RightLimitSwitchDisable = 12
        LeftLimitSwitchDisable = 13
        SwapLimitSwitches = 14
        A1 = 15
        V1 = 16
        MaxDeceleration = 17
        D1 = 18
        StartVelocity = 19
        StopVelocity = 20
        RampWaitTime = 21
        HighSpeedTheshold = 22
        MinDcStepSpeed = 23
        RightLimitSwitchPolarity = 24
        LeftLimitSwitchPolarity = 25
        SoftStop = 26
        HighSpeedChopperMode = 27
        HighSpeedFullstepMode = 28
        MeasuredSpeed = 29
        PowerDownRamp = 31
        DcStepTime = 32
        DcStepStallGuard = 33
        RelativePositioningOption = 127
        MicrostepResolution = 140
        Intpol = 160
        DoubleEdgeSteps = 161
        ChopperBlankTime = 162
        ConstantTOffMode = 163
        DisableFastDecayComparator = 164
        ChopperHysteresisEnd = 165
        ChopperHysteresisStart = 166
        TOff = 167
        SEIMIN = 168
        SECDS = 169
        SmartEnergyHysteresis = 170
        SECUS = 171
        SmartEnergyHysteresisStart = 172
        SG2FilterEnable = 173
        SG2Threshold = 174
        GlobalCurrentScaler = 178
        SmartEnergyActualCurrent = 180
        SmartEnergyStallVelocity = 181
        SmartEnergyThresholdSpeed = 182
        RandomTOffMode = 184
        ChopperSynchronization = 185
        PWMThresholdSpeed = 186
        PWMGrad = 187
        PWMAmplitude = 188
        PWMScale = 189
        PWMMode = 190
        PWMFrequency = 191
        PWMAutoscale = 192
        ReferenceSearchMode = 193
        ReferenceSearchSpeed = 194
        ReferenceSwitchSpeed = 195
        ReferenceSwitchDistance = 196
        LastReferenceSwitchPosition = 197
        FullstepResolution = 202
        FreewheelingMode = 204
        LoadValue = 206
        ErrorFlags = 207  # ExtendedErrorFlags
        StatusFlags = 208  # DrvStatusFlags
        EncoderPosition = 209
        EncoderClearOnNull = 210
        MaxEncoderDeviation = 212
        PowerDownDelay = 214
        AbsoluteResolverValue = 215
        ExternalEncoderPosition = 216
        ExternalEncoderResolution = 217
        ExternalEncoderMaxDeviation = 218
        ReverseShaft = 251
        StepDirectionMode = 254
        UnitMode = 255

    class MicroStepResolution:
        MicrostepResolutionFullstep = 0
        MicrostepResolutionHalfstep = 1
        MicrostepResolution4Microsteps = 2
        MicrostepResolution8Microsteps = 3
        MicrostepResolution16Microsteps = 4
        MicrostepResolution32Microsteps = 5
        MicrostepResolution64Microsteps = 6
        MicrostepResolution128Microsteps = 7
        MicrostepResolution256Microsteps = 8

    class GP0:
        SerialBaudRate = 65
        SerialAddress = 66
        SerialHeartbeat = 68
        CANBitRate = 69
        CANsendID = 70
        CANreceiveID = 71
        TelegramPauseTime = 75
        SerialHostAddress = 76
        AutoStartMode = 77
        ProtectionMode = 81
        CANHeartbeat = 82
        CANSecondaryAddress = 83
        EepromCoordinateStore = 84
        ZeroUserVariables = 85
        SerialSecondaryAddress = 87
        ApplicationStatus = 128
        DownloadMode = 129
        ProgramCounter = 130
        TickTimer = 132
        RandomNumber = 133
        SuppressReply = 255

    class GP3:
        Timer_0 = 0
        Timer_1 = 1
        Timer_2 = 2
        StopLeft_0 = 27
        StopRight_0 = 28
        Input_0 = 39
        Input_1 = 40
        Input_2 = 41
        Input_3 = 42

    class IO:
        OUT0 = 0
        OUT1 = 1
        IN0 = 0
        IN1 = 1
