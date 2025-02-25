use defmt::Format;

#[derive(Debug, PartialEq, Eq, Format)]
#[repr(u8)]
pub enum CanCommandIds {
    GetState = 0x01,
    SetRelays = 0x02,
}

impl CanCommandIds {
    pub fn from_u8(value: u8) -> Option<CanCommandIds> {
        match value {
            0x01 => Some(CanCommandIds::GetState),
            0x02 => Some(CanCommandIds::SetRelays),
            _ => None,
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
#[repr(u8)]
pub enum CanResponseIds {
    Invalid = 0x00,
    State = 0x01,
    Relays = 0x02,
}

pub enum Messages {
    GetState,
    SetRelays(u8),
}

#[derive(Debug, Copy, Clone, Format)]
pub struct BoardState {
    pub relays: u8,
}

impl Messages {
    pub fn from_can_frame(frame: &[u8]) -> Option<Messages> {
        if frame.len() < 1 {
            return None;
        }
        if let Some(msg_id) = CanCommandIds::from_u8(frame[0]) {
            match msg_id {
                CanCommandIds::GetState => {
                    return Some(Messages::GetState);
                }
                CanCommandIds::SetRelays => {
                    if frame.len() < 2 {
                        return None;
                    }
                    return Some(Messages::SetRelays(frame[1] & 0b1111));
                }
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
                data[1] = board_state.relays;
                data
            }
            Self::Invalid => [0u8; 8],
        }
    }
}
