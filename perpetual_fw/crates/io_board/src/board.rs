use embassy_embedded_hal::shared_bus::asynch::spi::SpiDevice;
use embassy_executor::Spawner;
use embassy_stm32::can::{enums::BusError, frame::Envelope, Frame};
use embassy_stm32::gpio::{Level, Output, Pull, Speed};
use embassy_stm32::mode::Async;
use embassy_stm32::time::Hertz;
use embassy_stm32::Config;
use embassy_stm32::{bind_interrupts, can, peripherals};
use embassy_sync::blocking_mutex::raw::{CriticalSectionRawMutex, ThreadModeRawMutex};
use embassy_sync::channel::Channel;
use embassy_sync::mutex::Mutex;
use static_cell::StaticCell;

use crate::core::{BoardState, Messages, Responses};

pub struct IoBoard<'a> {
    // Wedge 1
    pub wedge_1: Output<'a>,
    // Wedge 2
    pub wedge_2: Output<'a>,
    // Wedge 3
    pub wedge_3: Output<'a>,
    // Actuators
    pub actuators: Output<'a>,
    pub actuator_state: bool,
    pub wedge_state: u8,
}

bind_interrupts!(struct Irqs {
    FDCAN1_IT0 => can::IT0InterruptHandler<peripherals::FDCAN1>;
    FDCAN1_IT1 => can::IT1InterruptHandler<peripherals::FDCAN1>;
});

//static COMMAND_CHANNEL: Channel<ThreadModeRawMutex, [u8; 8], 64> = Channel::new();
//static RESPONSE_CHANNEL: Channel<ThreadModeRawMutex, [u8; 8], 64> = Channel::new();

static COMMAND_CHANNEL: Channel<ThreadModeRawMutex, Messages, 64> = Channel::new();
static RESPONSE_CHANNEL: Channel<ThreadModeRawMutex, Responses, 64> = Channel::new();

#[embassy_executor::task]
async fn can_receive_task(
    can_id: embedded_can::Id,
    mut can: embassy_stm32::can::BufferedCan<'static, 64, 64>,
) -> ! {
    loop {
        let res = can.read().await;
        if let Ok(ref e) = res {
            if *e.frame.id() == can_id {
                if let Some(msg) = Messages::from_can_frame(e.frame.data()) {
                    COMMAND_CHANNEL.send(msg).await;
                    let response = RESPONSE_CHANNEL.receive().await;
                    can.write(Frame::new_data(can_id, &response.into_can_frame()).unwrap())
                        .await;
                } else {
                    can.write(
                        Frame::new_data(can_id, &Responses::Invalid.into_can_frame()).unwrap(),
                    )
                    .await;
                }
            }
        }
    }
}

impl<'a> IoBoard<'a> {
    pub fn new(can_id: embedded_can::Id, config: Config, spawner: Spawner) -> Self {
        let p = embassy_stm32::init(config);
        let mut can = can::CanConfigurator::new(p.FDCAN1, p.PA11, p.PA12, Irqs);
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
        let _ = spawner.spawn(can_receive_task(can_id, buffered_can));

        let wedge_1 = Output::new(p.PC5, Level::Low, Speed::Low);
        let wedge_2 = Output::new(p.PC4, Level::Low, Speed::Low);
        let wedge_3 = Output::new(p.PA10, Level::Low, Speed::Low);
        let actuators = Output::new(p.PB3, Level::Low, Speed::Low);

        Self {
            wedge_1,
            wedge_2,
            wedge_3,
            actuators,
            actuator_state: 0,
            wedge_state: 0,
        }
    }

    fn reset_all_valves(&mut self) {
        self.wedge_1.set_low();
        self.wedge_2.set_low();
        self.wedge_3.set_low();
        self.actuators.set_low();
        self.actuator_state = false;
        self.wedge_state = 0;
    }

    fn set_all_valves(&mut self) {
        self.wedge_1.set_high();
        self.wedge_2.set_high();
        self.wedge_3.set_high();
        self.actuators.set_high();
        self.actuator_state = true;
        self.wedge_state = 0b111;
    }

    fn set_actuators(&mut self, state: bool) {
        if state {
            self.actuators.set_high();
        } else {
            self.actuators.set_low();
        }
        self.actuator_state = state;
    }

    fn set_wedges(&mut self, state: u8) {
        if state & 0b1 {
            self.wedge_1.set_high();
        } else {
            self.wedge_1.set_low();
        }
        if state & 0b01 {
            self.wedge_2.set_high();
        } else {
            self.wedge_2.set_low();
        }
        if state & 0b001 {
            self.wedge_3.set_high();
        } else {
            self.wedge_3.set_low();
        }
        self.wedge_state = state;
    }

    fn get_state(&mut self) -> BoardState {
        BoardState {
            wedges: self.wedge_state,
            actuators: self.actuator_state,
        }
    }

    pub fn handle_message(msg: Messages) -> Responses {
        match msg {
            Messages::GetState => {}
            Messages::SetWedges(state) => {
                self.set_wedges(state);
                Responses::State(self.get_state())
            }
            Messages::SetActuators(state) => {
                self.set_actuators(state);
                Responses::State(self.get_state())
            }
            Messages::ResetValves => {
                self.reset_all_valves();
                Responses::State(self.get_state())
            }
            Messages::SetAllValves => {
                self.set_all_valves();
                Responses::State(self.get_state())
            }
        }
    }

    pub async fn run(&mut self) -> ! {
        loop {
            let cmd = COMMAND_CHANNEL.receive().await;
            let response = self.handle_message(cmd);
            RESPONSE_CHANNEL.send(response).await;
        }
    }
}
