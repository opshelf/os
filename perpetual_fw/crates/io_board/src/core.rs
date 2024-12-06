use bitfield::bitfield;

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

#[derive(Debug, PartialEq, Eq)]
#[repr(u8)]
pub enum CanCommandIds {
    GetState = 0x01,
    SetWedges = 0x02,
    SetActuators = 0x03,
    ResetValves = 0x04,
    SetAllValves = 0x05,
}

impl CanCommandIds {
    pub fn from_u8(value: u8) -> Option<CanCommandIds> {
        match value {
            0x01 => Some(CanCommandIds::GetState),
            0x02 => Some(CanCommandIds::SetWedges),
            0x03 => Some(CanCommandIds::SetActuators),
            0x04 => Some(CanCommandIds::ResetValves),
            0x05 => Some(CanCommandIds::SetAllValves),
            _ => None,
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
#[repr(u8)]
pub enum CanResponseIds {
    Invalid = 0x00,
    State = 0x01,
}

pub enum Messages {
    GetState,
    SetWedges(u8),
    SetActuators(bool),
    ResetValves,
    SetAllValves,
}

#[derive(Debug, Copy, Clone, Format)]
pub struct BoardState {
    pub wedges: u8,
    pub actuators: bool,
}

impl Messages {
    pub fn from_can_frame(frame: &[u8]) -> Option<Messages> {
        if frame.len() < 1 {
            return None;
        }
        if let Some(msg_id) = CanCommandIds::from_u8(frame[0]) {
            match msg_id {
                CanCommandIds::GetState => {}
                CanCommandIds::SetWedges => {
                    if frame.len() < 2 {
                        return None;
                    }
                    return Some(Messages::SetWedges(frame[1] & 0b111));
                }
                CanCommandIds::SetActuators => {
                    if frame.len() < 2 {
                        return None;
                    }
                    return Some(Messages::SetActuators(bool::from(frame[1])));
                }
                CanCommandIds::ResetValves => {
                    return Some(Messages::ResetValves);
                }
                CanCommandIds::SetAllValves => {
                    return Some(Messages::SetAllValves);
                }
                _ => None,
            }
        } else {
            return None;
        }
    }
}

pub enum Responses {
    State(BoardState),
    Invalid,
}

impl Responses {
    pub fn into_can_frame(&self) -> [u8; 8] {
        match self {
            Self::State(board_state) => {
                let mut data = [0u8; 8];
                data[0] = CanResponseIds::State as u8;
                data[1] = board_state.wedges;
                data[2] = board_state.actuators as u8;
                data
            }
            Self::Invalid => [0; 8],
        }
    }
}
