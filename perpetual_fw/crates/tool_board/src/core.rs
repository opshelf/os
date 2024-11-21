use bitfield::bitfield;

bitfield! {
    pub struct SlotStatus(u8);
    impl Debug;
    pub limit_2, _: 7;
    pub limit_1, _: 6;
    pub slot_6, _: 5;
    pub slot_5, _: 4;
    pub slot_4, _: 3;
    pub slot_3, _: 2;
    pub slot_2, _: 1;
    pub slot_1, _: 0;
}

pub enum MotorCommands {
    HomeMotor,
    ResetMotor,
    Initialize,
    GetMotorPosition,
    MoveAbsolute(i32),
    MoveRealtive(i32),
    SetMotorCurrent(u8),
    GetMotorStatus,
}

pub enum MotorResponses {
    Homed,
    Reset,
    Initialized,
    Position(i32),
    MoveAbsolute(i32),
    MoveRelative(i32),
    MotorCurrent(u8),
    MotorStatus(u32),
}

bitfield! {
    pub struct CarriageState(u8);
    impl Debug;
    pub solenoid_6, _: 5;
    pub solenoid_5, _: 4;
    pub solenoid_4, _: 3;
    pub solenoid_3, _: 2;
    pub solenoid_2, _: 1;
    pub solenoid_1, _: 0;
}

bitfield! {
    pub struct PullerState(u8);
    impl Debug;
    pub solenoid_6, _: 5;
    pub solenoid_5, _: 4;
    pub solenoid_4, _: 3;
    pub solenoid_3, _: 2;
    pub solenoid_2, _: 1;
    pub solenoid_1, _: 0;
}

pub enum PullerCommands {
    SetPullers(PullerState),
    ReadPullers,
}

pub enum PullerResponse {
    Pullers(PullerState),
    Invalid,
}

pub enum CarriageCommands {
    SetCarriage(PullerState),
    ReadPullers,
}

pub enum CarriageResponse {
    Pullers(PullerState),
    Invalid,
}

pub enum Messages {
    MotorCommand(MotorCommands),
    PullerCommand(PullerCommands),
    CarriageCommand(CarriageCommands),
}

pub enum Responses {
    MotorResponse(MotorCommands),
    PullerResponse(PullerResponse),
    CarriageResponse(CarriageResponse),
}
