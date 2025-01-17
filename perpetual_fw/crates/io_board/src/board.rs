use embassy_executor::Spawner;
use embassy_futures::select::Either5;
use embassy_stm32::can::{enums::BusError, frame::Envelope, Frame};
use embassy_stm32::exti::ExtiInput;
use embassy_stm32::gpio::{Level, Output, Pull, Speed};
use embassy_stm32::Config;
use embassy_stm32::{bind_interrupts, can, peripherals};
use embassy_sync::blocking_mutex::raw::{CriticalSectionRawMutex, ThreadModeRawMutex};
use embassy_sync::channel::Channel;
use embedded_can::StandardId;
use static_cell::StaticCell;

use crate::core::{BoardState, Messages, Responses, Sensors};

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

static COMMAND_CHANNEL: Channel<ThreadModeRawMutex, Messages, 64> = Channel::new();
static RESPONSE_CHANNEL: Channel<ThreadModeRawMutex, Responses, 64> = Channel::new();
static SENSOR_CHANNEL: Channel<ThreadModeRawMutex, Sensors, 64> = Channel::new();

#[embassy_executor::task]
async fn break_beam_monitor(
    mut input_1: ExtiInput<'static>,
    mut input_2: ExtiInput<'static>,
    mut input_3: ExtiInput<'static>,
    mut input_4: ExtiInput<'static>,
    mut input_5: ExtiInput<'static>,
) -> ! {
    loop {
        let result = embassy_futures::select::select5(
            input_1.wait_for_falling_edge(),
            input_2.wait_for_falling_edge(),
            input_3.wait_for_falling_edge(),
            input_4.wait_for_falling_edge(),
            input_5.wait_for_falling_edge(),
        )
        .await;
        const DEBOUNCE: u64 = 2;
        let beam_value = match result {
            Either5::First(_) => {
                //defmt::info!("Input1");
                let t0 = embassy_time::Instant::now();
                let mut invalid = false;
                while (embassy_time::Instant::now() - t0).as_millis() < DEBOUNCE {
                    if input_1.is_high() {
                        invalid = true;
                        break;
                    }
                    embassy_time::Timer::after_millis(1).await;
                }
                if invalid {
                    None
                } else {
                    input_1.wait_for_high().await;
                    Some(Sensors {
                        output_conveyor: true,
                        ..Default::default()
                    })
                }
            }
            Either5::Second(_) => {
                //defmt::info!("Input2");
                //input_2.wait_for_high().await;
                //Sensors {
                //    input_conveyor: true,
                //    ..Default::default()
                //}

                let t0 = embassy_time::Instant::now();
                let mut invalid = false;
                while (embassy_time::Instant::now() - t0).as_millis() < DEBOUNCE {
                    if input_2.is_high() {
                        invalid = true;
                        break;
                    }
                    embassy_time::Timer::after_millis(1).await;
                }
                if invalid {
                    None
                } else {
                    input_2.wait_for_high().await;
                    Some(Sensors {
                        input_conveyor: true,
                        ..Default::default()
                    })
                }
            }
            Either5::Third(_) => {
                //defmt::info!("Input3");
                //input_3.wait_for_high().await;
                //Sensors {
                //    output_transfer_conveyor: true,
                //    ..Default::default()
                //}
                let t0 = embassy_time::Instant::now();
                let mut invalid = false;
                while (embassy_time::Instant::now() - t0).as_millis() < DEBOUNCE {
                    if input_3.is_high() {
                        invalid = true;
                        break;
                    }
                    embassy_time::Timer::after_millis(1).await;
                }
                if invalid {
                    None
                } else {
                    input_3.wait_for_high().await;
                    Some(Sensors {
                        output_transfer_conveyor: true,
                        ..Default::default()
                    })
                }
            }
            Either5::Fourth(_) => {
                //defmt::info!("Input4");
                //input_4.wait_for_high().await;
                //Sensors {
                //    funnel_input: true,
                //    ..Default::default()
                //}
                let t0 = embassy_time::Instant::now();
                let mut invalid = false;
                while (embassy_time::Instant::now() - t0).as_millis() < DEBOUNCE {
                    if input_4.is_high() {
                        invalid = true;
                        break;
                    }
                    embassy_time::Timer::after_millis(1).await;
                }
                if invalid {
                    None
                } else {
                    input_4.wait_for_high().await;
                    Some(Sensors {
                        funnel_input: true,
                        ..Default::default()
                    })
                }
            }
            Either5::Fifth(_) => {
                //defmt::info!("Input5");
                //input_5.wait_for_high().await;
                //Sensors {
                //    output_box: true,
                //    ..Default::default()
                //}
                let t0 = embassy_time::Instant::now();
                let mut invalid = false;
                while (embassy_time::Instant::now() - t0).as_millis() < DEBOUNCE {
                    if input_5.is_high() {
                        invalid = true;
                        break;
                    }
                    embassy_time::Timer::after_millis(1).await;
                }
                if invalid {
                    None
                } else {
                    input_5.wait_for_high().await;
                    Some(Sensors {
                        output_box: true,
                        ..Default::default()
                    })
                }
            }
        };

        // Debounce
        if let Some(bv) = beam_value {
            SENSOR_CHANNEL.send(bv).await;
            embassy_time::Timer::after_millis(100);
        }

        //defmt::info!(
        //    "{} {} {} {} {}",
        //    input_1.is_low(),
        //    input_2.is_low(),
        //    input_3.is_low(),
        //    input_4.is_low(),
        //    input_5.is_low()
        //);
        //embassy_time::Timer::after_millis(100).await
    }
}

#[embassy_executor::task]
async fn can_receive_task(
    receive_id: embedded_can::Id,
    reply_id: embedded_can::Id,
    mut can: embassy_stm32::can::BufferedCan<'static, 64, 64>,
) -> ! {
    loop {
        let res = can.read().await;
        if let Ok(ref e) = res {
            if *e.frame.id() == receive_id {
                if let Some(msg) = Messages::from_can_frame(e.frame.data()) {
                    let t0 = embassy_time::Instant::now();
                    COMMAND_CHANNEL.send(msg).await;
                    let response = RESPONSE_CHANNEL.receive().await;
                    can.write(Frame::new_data(reply_id, &response.into_can_frame()).unwrap())
                        .await;
                } else {
                    can.write(
                        Frame::new_data(reply_id, &Responses::Invalid.into_can_frame()).unwrap(),
                    )
                    .await;
                }
            }
        }
    }
}

#[embassy_executor::task]
async fn watchdog_task(
    mut wdg: embassy_stm32::wdg::IndependentWatchdog<'static, embassy_stm32::peripherals::IWDG>,
) -> ! {
    wdg.unleash();
    loop {
        wdg.pet();
        embassy_time::Timer::after_millis(100).await;
    }
}

impl<'a> IoBoard<'a> {
    pub fn new(
        can_receive_id: embedded_can::Id,
        can_reply_id: embedded_can::Id,
        config: Config,
        spawner: Spawner,
    ) -> Self {
        defmt::info!("Resetting");
        let p = embassy_stm32::init(config);
        let mut can = can::CanConfigurator::new(p.FDCAN1, p.PB8, p.PB9, Irqs);
        let mut filter = can::filter::StandardFilter::default();
        filter.filter = embassy_stm32::can::filter::FilterType::DedicatedSingle(
            embedded_can::StandardId::new(100).unwrap(),
        );
        can.properties()
            .set_standard_filter(can::filter::StandardFilterSlot::_0, filter);
        can.set_bitrate(500_000);
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
        let wdg = embassy_stm32::wdg::IndependentWatchdog::new(p.IWDG, 500_000);
        let _ = spawner.spawn(can_receive_task(can_receive_id, can_reply_id, buffered_can));
        let _ = spawner.spawn(watchdog_task(wdg));

        let actuators = Output::new(p.PC5, Level::Low, Speed::Low);
        let wedge_1 = Output::new(p.PB3, Level::Low, Speed::Low);
        let wedge_2 = Output::new(p.PA10, Level::Low, Speed::Low);
        let wedge_3 = Output::new(p.PC4, Level::Low, Speed::Low);

        // Output Conveyor
        let input_1 = ExtiInput::new(p.PB10, p.EXTI10, Pull::Up);

        // Input Conveyor
        let input_2 = ExtiInput::new(p.PA8, p.EXTI8, Pull::Up);

        // Output Transfer Conveyor
        let input_3 = ExtiInput::new(p.PA9, p.EXTI9, Pull::Up);

        // Funnel Input
        let input_4 = ExtiInput::new(p.PC7, p.EXTI7, Pull::Up);

        // Output Box
        let input_5 = ExtiInput::new(p.PB6, p.EXTI6, Pull::Up);

        let _ = spawner.spawn(break_beam_monitor(
            input_1, input_2, input_3, input_4, input_5,
        ));

        Self {
            wedge_1,
            wedge_2,
            wedge_3,
            actuators,
            actuator_state: false,
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
        if state & 0b1 > 0 {
            self.wedge_1.set_high();
        } else {
            self.wedge_1.set_low();
        }
        if state & 0b10 > 0 {
            self.wedge_2.set_high();
        } else {
            self.wedge_2.set_low();
        }
        if state & 0b100 > 0 {
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

    pub fn handle_message(&mut self, msg: Messages) -> Responses {
        match msg {
            Messages::GetState => Responses::State(self.get_state()),
            Messages::GetSensors => match SENSOR_CHANNEL.try_receive() {
                Ok(res) => Responses::Sensors(Some(res)),
                _ => Responses::Sensors(None),
            },
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
            Messages::ClearState => {
                while let Ok(sensors) = SENSOR_CHANNEL.try_receive() {}
                defmt::info!("Done");
                Responses::Sensors(None)
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
