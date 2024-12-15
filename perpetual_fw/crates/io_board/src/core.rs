use defmt::Format;

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

#[derive(Debug, PartialEq, Eq, Format)]
#[repr(u8)]
pub enum CanCommandIds {
    GetState = 0x01,
    SetWedges = 0x02,
    SetActuators = 0x03,
    ResetValves = 0x04,
    SetAllValves = 0x05,
    GetSensors = 0x06,
    ClearState = 0x07,
}

impl CanCommandIds {
    pub fn from_u8(value: u8) -> Option<CanCommandIds> {
        match value {
            0x01 => Some(CanCommandIds::GetState),
            0x02 => Some(CanCommandIds::SetWedges),
            0x03 => Some(CanCommandIds::SetActuators),
            0x04 => Some(CanCommandIds::ResetValves),
            0x05 => Some(CanCommandIds::SetAllValves),
            0x06 => Some(CanCommandIds::GetSensors),
            0x07 => Some(CanCommandIds::ClearState),
            _ => None,
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
#[repr(u8)]
pub enum CanResponseIds {
    Invalid = 0x00,
    State = 0x01,
    Sensors = 0x02,
}

pub enum Messages {
    GetState,
    SetWedges(u8),
    SetActuators(bool),
    ResetValves,
    SetAllValves,
    GetSensors,
    ClearState,
}

#[derive(Debug, Copy, Clone, Format)]
pub struct BoardState {
    pub wedges: u8,
    pub actuators: bool,
}

#[derive(Debug, Copy, Clone, Format)]
pub struct Sensors {
    pub output_conveyor: bool,
    pub input_conveyor: bool,
    pub output_transfer_conveyor: bool,
    pub funnel_input: bool,
    pub output_box: bool,
}

impl Sensors {
    pub fn as_u8(&self) -> u8 {
        let mut data = 0u8;
        data |= (self.output_conveyor as u8) << 0;
        data |= (self.input_conveyor as u8) << 1;
        data |= (self.output_transfer_conveyor as u8) << 2;
        data |= (self.funnel_input as u8) << 3;
        data |= (self.output_box as u8) << 4;
        data
    }
}

impl core::default::Default for Sensors {
    fn default() -> Sensors {
        Self {
            output_conveyor: false,
            input_conveyor: false,
            output_transfer_conveyor: false,
            funnel_input: false,
            output_box: false,
        }
    }
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
                CanCommandIds::GetSensors => {
                    return Some(Messages::GetSensors);
                }
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
                    return Some(Messages::SetActuators(frame[1] > 0));
                }
                CanCommandIds::ResetValves => {
                    return Some(Messages::ResetValves);
                }
                CanCommandIds::SetAllValves => {
                    return Some(Messages::SetAllValves);
                }
                CanCommandIds::ClearState => return Some(Messages::ClearState),
            }
        } else {
            return None;
        }
    }
}

pub enum Responses {
    State(BoardState),
    Sensors(Option<Sensors>),
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
            Self::Sensors(maybe_sensors) => match maybe_sensors {
                Some(sensors) => {
                    let mut data = [0u8; 8];
                    data[0] = CanResponseIds::Sensors as u8;
                    data[1] = sensors.as_u8();
                    data[2] = 1;
                    data
                }
                None => {
                    let mut data = [0u8; 8];
                    data[0] = CanResponseIds::Sensors as u8;
                    data[1] = 0;
                    data[2] = 0;
                    data
                }
            },
            Self::Invalid => [0u8; 8],
        }
    }
}
