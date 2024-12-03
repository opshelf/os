#[allow(unused_imports)]
#[cfg(feature = "defmt")]
use defmt::Format;

#[derive(Copy, Clone, Eq, PartialEq)]
#[cfg_attr(feature = "defmt", derive(defmt::Format))]
#[repr(u8)]
enum CommandIds {
    MaskBits = 0x01,
    SetBits = 0x02,
    ReadBits = 0x03,
    Invalid = 0x04,
}

impl TryFrom<u8> for CommandIds {
    type Error = ();
    fn try_from(value: u8) -> Result<Self, Self::Error> {
        match value {
            0x01 => Ok(CommandIds::MaskBits),
            0x02 => Ok(CommandIds::SetBits),
            0x03 => Ok(CommandIds::ReadBits),
            0x04 => Ok(CommandIds::Invalid),
            _ => Err(()),
        }
    }
}

#[derive(Copy, Clone, Eq, PartialEq)]
#[cfg_attr(feature = "defmt", derive(defmt::Format))]
pub enum RelayCommands {
    MaskBits(u16),
    SetBits(u16),
    ReadBits,
}

impl RelayCommands {
    pub fn serialize_to_frame(&self) -> [u8; 8] {
        let mut buffer: [u8; 8] = [0u8; 8];
        match self {
            RelayCommands::MaskBits(state) => {
                let bytes = state.to_le_bytes();
                buffer[0] = CommandIds::MaskBits as u8;
                buffer[1] = bytes[0];
                buffer[2] = bytes[1];
            }
            RelayCommands::SetBits(state) => {
                let bytes = state.to_le_bytes();
                buffer[0] = CommandIds::SetBits as u8;
                buffer[1] = bytes[0];
                buffer[2] = bytes[1];
            }
            RelayCommands::ReadBits => {
                buffer[0] = CommandIds::ReadBits as u8;
            }
        }
        buffer
    }
}

#[derive(Copy, Clone, Eq, PartialEq)]
#[cfg_attr(feature = "defmt", derive(defmt::Format))]
pub enum RelayResponses {
    MaskBits(u8, u8),
    SetBits(u8, u8),
    ReadBits(u8, u8),
    Invalid,
}

impl RelayResponses {
    pub fn serialize_to_frame(&self) -> [u8; 8] {
        let mut buffer: [u8; 8] = [0u8; 8];
        match self {
            RelayResponses::MaskBits(bank1, bank2) => {
                buffer[0] = CommandIds::MaskBits as u8;
                buffer[1] = *bank1;
                buffer[2] = *bank2;
            }
            RelayResponses::SetBits(bank1, bank2) => {
                buffer[0] = CommandIds::SetBits as u8;
                buffer[1] = *bank1;
                buffer[2] = *bank2;
            }
            RelayResponses::ReadBits(bank1, bank2) => {
                buffer[0] = CommandIds::ReadBits as u8;
                buffer[1] = *bank1;
                buffer[2] = *bank2;
            }
            RelayResponses::Invalid => {
                buffer[0] = CommandIds::Invalid as u8;
            }
        }
        buffer
    }
}

pub fn parse_relay_command(frame_data: &[u8]) -> Option<RelayCommands> {
    if frame_data.len() < 3 {
        return None;
    }
    let payload: [u8; 2] = frame_data[1..3].try_into().unwrap();
    if let Ok(cmd_id) = frame_data[0].try_into() {
        match cmd_id {
            CommandIds::MaskBits => Some(RelayCommands::MaskBits(u16::from_le_bytes(payload))),
            CommandIds::SetBits => Some(RelayCommands::SetBits(u16::from_le_bytes(payload))),
            CommandIds::ReadBits => Some(RelayCommands::ReadBits),
            _ => None,
        }
    } else {
        return None;
    }
}
