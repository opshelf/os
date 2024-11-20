use defmt::Format;
use embassy_executor::Spawner;
use embassy_stm32::can::enums::BusError;
use embassy_stm32::can::frame::Envelope;
use embassy_stm32::can::Frame;
use embassy_stm32::gpio::{Level, Output, Speed};
use embassy_stm32::Config;
use embassy_stm32::{bind_interrupts, can, peripherals, usart};
use embassy_sync::blocking_mutex::raw::{CriticalSectionRawMutex, ThreadModeRawMutex};
use embassy_sync::channel::Channel;
use static_cell::StaticCell;

pub struct NucleoG474<'a> {
    bank_1: u8,
    bank_2: u8,
    actuator_forward: Output<'a>,
    actuator_reverse: Output<'a>,
    slot_1: Output<'a>,
    slot_2: Output<'a>,
    slot_3: Output<'a>,
    slot_4: Output<'a>,
    slot_5: Output<'a>,
    slot_6: Output<'a>,
    puller_1: Output<'a>,
    puller_2: Output<'a>,
    puller_3: Output<'a>,
    puller_4: Output<'a>,
    puller_5: Output<'a>,
    puller_6: Output<'a>,
}

bind_interrupts!(struct Irqs {
    LPUART1 => usart::InterruptHandler<peripherals::LPUART1>;
    FDCAN1_IT0 => can::IT0InterruptHandler<peripherals::FDCAN1>;
    FDCAN1_IT1 => can::IT1InterruptHandler<peripherals::FDCAN1>;
});

#[derive(Copy, Clone, Eq, PartialEq, Format)]
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

#[derive(Copy, Clone, Eq, PartialEq, Format)]
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

#[derive(Copy, Clone, Eq, PartialEq, Format)]
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

fn parse_relay_command(frame_data: &[u8]) -> Option<RelayCommands> {
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

static COMMAND_CHANNEL: Channel<ThreadModeRawMutex, [u8; 8], 64> = Channel::new();
static RESPONSE_CHANNEL: Channel<ThreadModeRawMutex, [u8; 8], 64> = Channel::new();

#[embassy_executor::task]
async fn can_receive_task(
    can_id: embedded_can::Id,
    mut can: embassy_stm32::can::BufferedCan<'static, 64, 64>,
) -> ! {
    loop {
        let res = can.read().await;
        if let Ok(ref e) = res {
            if *e.frame.id() == can_id {
                let mut cmd: [u8; 8] = [0; 8];
                for (ix, i) in e.frame.data().into_iter().enumerate() {
                    cmd[ix] = *i;
                }
                COMMAND_CHANNEL.send(cmd).await;
                let response = RESPONSE_CHANNEL.receive().await;
                can.write(Frame::new_data(can_id, &response).unwrap()).await;
            }
        }
    }
}

impl<'a> NucleoG474<'a> {
    pub fn new(can_id: embedded_can::Id, config: Config, spawner: Spawner) -> Self {
        let p = embassy_stm32::init(config);
        let mut can = can::CanConfigurator::new(p.FDCAN1, p.PB8, p.PB9, Irqs);
        let mut filter = can::filter::StandardFilter::default();
        filter.filter = embassy_stm32::can::filter::FilterType::DedicatedSingle(
            embedded_can::StandardId::new(100).unwrap(),
        );
        can.properties()
            .set_standard_filter(can::filter::StandardFilterSlot::_0, filter);
        can.set_bitrate(1_000_000);
        let can_handle = can.into_normal_mode();
        static CAN_TX_CHANNEL: StaticCell<Channel<CriticalSectionRawMutex, Frame, 64>> =
            StaticCell::new();
        static CAN_RX_CHANNEL: StaticCell<
            Channel<CriticalSectionRawMutex, Result<Envelope, BusError>, 64>,
        > = StaticCell::new();
        let buffered_can = can_handle.buffered(
            CAN_TX_CHANNEL.init(Channel::new()),
            CAN_RX_CHANNEL.init(Channel::new()),
        );

        let mut actuator_forward = Output::new(p.PA5, Level::High, Speed::Low);
        let mut actuator_reverse = Output::new(p.PA6, Level::High, Speed::Low);
        let mut slot_1 = Output::new(p.PA7, Level::High, Speed::Low);
        let mut slot_2 = Output::new(p.PB6, Level::High, Speed::Low);
        let mut slot_3 = Output::new(p.PC7, Level::High, Speed::Low);
        let mut slot_4 = Output::new(p.PA9, Level::High, Speed::Low);
        let mut slot_5 = Output::new(p.PA8, Level::High, Speed::Low);
        let mut slot_6 = Output::new(p.PB10, Level::High, Speed::Low);
        let mut puller_1 = Output::new(p.PB4, Level::High, Speed::Low);
        let mut puller_2 = Output::new(p.PB5, Level::High, Speed::Low);
        let mut puller_3 = Output::new(p.PB3, Level::High, Speed::Low);
        let mut puller_4 = Output::new(p.PA10, Level::High, Speed::Low);
        let mut puller_5 = Output::new(p.PC4, Level::High, Speed::Low);
        let mut puller_6 = Output::new(p.PC5, Level::High, Speed::Low);

        let _ = spawner.spawn(can_receive_task(can_id, buffered_can));
        actuator_forward.set_high();
        actuator_reverse.set_high();
        slot_1.set_high();
        slot_2.set_high();
        slot_3.set_high();
        slot_4.set_high();
        slot_5.set_high();
        slot_6.set_high();
        puller_1.set_high();
        puller_2.set_high();
        puller_3.set_high();
        puller_4.set_high();
        puller_5.set_high();
        puller_6.set_high();
        Self {
            bank_1: 0,
            bank_2: 0,
            actuator_forward,
            actuator_reverse,
            slot_1,
            slot_2,
            slot_3,
            slot_4,
            slot_5,
            slot_6,
            puller_1,
            puller_2,
            puller_3,
            puller_4,
            puller_5,
            puller_6,
        }
    }

    fn handle_command(&mut self, cmd: RelayCommands) -> RelayResponses {
        match cmd {
            RelayCommands::ReadBits => RelayResponses::ReadBits(self.bank_1, self.bank_2),
            RelayCommands::MaskBits(state) => {
                let bytes = state.to_le_bytes();
                self.bank_1 |= bytes[0];
                self.bank_2 |= bytes[1];
                RelayResponses::MaskBits(self.bank_1, self.bank_2)
            }
            RelayCommands::SetBits(state) => {
                let bytes = state.to_le_bytes();
                self.bank_1 = bytes[0];
                self.bank_2 = bytes[1];
                RelayResponses::SetBits(self.bank_1, self.bank_2)
            }
        }
    }

    fn set_bank_1(&mut self) {
        if self.bank_1 & 0x01 > 0 {
            self.actuator_forward.set_low();
        } else {
            self.actuator_forward.set_high();
        }
        if self.bank_1 & 0x02 > 0 {
            self.actuator_reverse.set_low();
        } else {
            self.actuator_reverse.set_high();
        }
        if self.bank_1 & 0x04 > 0 {
            self.slot_1.set_low();
        } else {
            self.slot_1.set_high();
        }
        if self.bank_1 & 0x08 > 0 {
            self.slot_2.set_low();
        } else {
            self.slot_2.set_high();
        }
        if self.bank_1 & 0x10 > 0 {
            self.slot_3.set_low();
        } else {
            self.slot_3.set_high();
        }
        if self.bank_1 & 0x20 > 0 {
            self.slot_4.set_low();
        } else {
            self.slot_4.set_high();
        }
        if self.bank_1 & 0x40 > 0 {
            self.slot_5.set_low();
        } else {
            self.slot_5.set_high();
        }
        if self.bank_1 & 0x80 > 0 {
            self.slot_6.set_low();
        } else {
            self.slot_6.set_high();
        }
    }

    fn set_bank_2(&mut self) {
        if self.bank_2 & 0x01 > 0 {
            self.puller_1.set_low();
        } else {
            self.puller_1.set_high();
        }
        if self.bank_2 & 0x02 > 0 {
            self.puller_2.set_low();
        } else {
            self.puller_2.set_high();
        }
        if self.bank_2 & 0x04 > 0 {
            self.puller_3.set_low();
        } else {
            self.puller_3.set_high();
        }
        if self.bank_2 & 0x08 > 0 {
            self.puller_4.set_low();
        } else {
            self.puller_4.set_high();
        }
        if self.bank_2 & 0x10 > 0 {
            self.puller_5.set_low();
        } else {
            self.puller_5.set_high();
        }
        if self.bank_2 & 0x20 > 0 {
            self.puller_6.set_low();
        } else {
            self.puller_6.set_high();
        }
        // if command[1] & 0x20 > 0 {
        // } else {
        // }
        // if command[1] & 0x40 > 0 {
        // } else {
        // }
        // if command[1] & 0x80 > 0 {
        // } else {
        // }
    }

    pub async fn run(&mut self) -> ! {
        loop {
            let command = COMMAND_CHANNEL.receive().await;
            if let Some(cmd) = parse_relay_command(&command) {
                let response = self.handle_command(cmd);
                self.set_bank_1();
                self.set_bank_2();
                RESPONSE_CHANNEL.send(response.serialize_to_frame()).await;
            } else {
                RESPONSE_CHANNEL
                    .send(RelayResponses::Invalid.serialize_to_frame())
                    .await;
            }
        }
    }
}
