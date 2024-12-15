use bitfield::bitfield;

const SOLENOID_BIT_MIASK: u8 = 0b00111111;

pub fn get_number_of_bits(value: u8) -> u8 {
    let mut accum = 0;
    for i in 0..8 {
        if value & (1 << i) != 0 {
            accum += 1;
        }
    }
    return accum;
}

pub fn valid_number_of_bits(value: u8, max_bits: u8) -> bool {
    get_number_of_bits(value) <= max_bits
}

//pub enum MotorCommands {
//    HomeMotor,
//    ResetMotor,
//    Initialize,
//    GetMotorPosition,
//    MoveAbsolute(i32),
//    MoveRealtive(i32),
//    SetMotorCurrent(u8),
//    GetMotorStatus,
//}
//
//pub enum MotorResponses {
//    Homed,
//    Reset,
//    Initialized,
//    Position(i32),
//    MoveAbsolute(i32),
//    MoveRelative(i32),
//    MotorCurrent(u8),
//    MotorStatus(u32),
//}

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

bitfield! {
    pub struct Inputs(u8);
    impl Debug;
    pub forward_actuator, set_forward_actuator: 5;
    pub backward_actuator, set_backward_actuator: 4;
    pub left_beam, set_left_beam: 3;
    pub right_beam, set_right_beam: 2;
    pub left_presence, set_left_presence: 1;
    pub right_presence, set_right_presence: 0;
}

#[derive(Debug)]
pub struct InputState {
    pub inputs: Inputs,
    pub slots: SlotStatus,
}

pub struct BoardState {
    pub inputs: InputState,
    pub puller: PullerState,
    pub carriage: CarriageState,
}

impl BoardState {
    pub fn to_can_data(&self) -> [u8; 4] {
        return [
            self.inputs.inputs.0,
            self.inputs.slots.0,
            self.puller.0,
            self.carriage.0,
        ];
    }
}

#[derive(Debug, PartialEq, Eq)]
#[repr(u8)]
pub enum CanCommandIds {
    GetState = 0x01,
    SetPullers = 0x02,
    MaskPullers = 0x03,
    SetCarriage = 0x04,
    MaskCarriage = 0x05,
    ResetAll = 0x06,
}

impl CanCommandIds {
    pub fn from_u8(value: u8) -> Option<CanCommandIds> {
        match value {
            0x01 => Some(CanCommandIds::GetState),
            0x02 => Some(CanCommandIds::SetPullers),
            0x03 => Some(CanCommandIds::MaskPullers),
            0x04 => Some(CanCommandIds::SetCarriage),
            0x05 => Some(CanCommandIds::MaskCarriage),
            0x06 => Some(CanCommandIds::ResetAll),
            _ => None,
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
#[repr(u8)]
pub enum CanResponseIds {
    Invalid = 0x00,
    GetState = 0x01,
    SetPullers = 0x02,
    MaskPullers = 0x03,
    ReadPullers = 0x04,
    SetCarriage = 0x05,
    MaskCarriage = 0x06,
    ReadCarriage = 0x07,
}

pub enum PullerCommands {
    SetPullers(PullerState),
    MaskPullers(PullerState),
}

pub enum PullerResponse {
    Pullers(PullerState),
    Invalid,
}

pub enum CarriageCommands {
    SetCarriage(CarriageState),
    MaskCarriage(CarriageState),
}

pub enum CarriageResponse {
    Pullers(PullerState),
    Invalid,
}

pub enum Messages {
    GetState,
    ResetAll,
    PullerCommand(PullerCommands),
    CarriageCommand(CarriageCommands),
}

impl Messages {
    pub fn from_can_frame(frame: &[u8]) -> Option<Messages> {
        if frame.len() < 1 {
            return None;
        }
        if let Some(msg_id) = CanCommandIds::from_u8(frame[0]) {
            if msg_id == CanCommandIds::GetState {
                Some(Messages::GetState)
            } else if msg_id == CanCommandIds::ResetAll {
                Some(Messages::ResetAll)
            } else if frame.len() < 2 {
                None
            } else {
                let data = frame[1] & SOLENOID_BIT_MIASK;
                if valid_number_of_bits(data, 3) {
                    match msg_id {
                        CanCommandIds::SetPullers => {
                            return Some(Messages::PullerCommand(PullerCommands::SetPullers(
                                PullerState(data),
                            )));
                        }
                        CanCommandIds::MaskPullers => {
                            return Some(Messages::PullerCommand(PullerCommands::MaskPullers(
                                PullerState(data),
                            )));
                        }
                        CanCommandIds::SetCarriage => {
                            return Some(Messages::CarriageCommand(CarriageCommands::SetCarriage(
                                CarriageState(data),
                            )));
                        }
                        CanCommandIds::MaskCarriage => {
                            return Some(Messages::CarriageCommand(
                                CarriageCommands::MaskCarriage(CarriageState(data)),
                            ));
                        }
                        _ => None,
                    }
                } else {
                    return None;
                }
            }
        } else {
            return None;
        }
    }
}

#[repr(u8)]
pub enum ResponseIds {
    Invalid = 0x00,
    State = 0x01,
}

pub enum Responses {
    State(BoardState),
    Invalid,
}

impl Responses {
    pub fn into_can_frame(&self) -> [u8; 8] {
        match self {
            Self::State(board_state) => {
                let response_data = board_state.to_can_data();
                let mut data = [0u8; 8];
                data[0] = ResponseIds::State as u8;
                for (ix, byte) in response_data.into_iter().enumerate() {
                    data[ix + 1] = byte;
                }
                data
            }
            Self::Invalid => [0; 8],
        }
    }
}
