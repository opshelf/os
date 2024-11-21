use embassy_executor::Spawner;
use embassy_stm32::can::enums::BusError;
use embassy_stm32::can::frame::Envelope;
use embassy_stm32::can::Frame;
use embassy_stm32::gpio::{Input, Level, Output, Pull, Speed};
use embassy_stm32::Config;
use embassy_stm32::{bind_interrupts, can, i2c, peripherals};
use embassy_sync::blocking_mutex::raw::{CriticalSectionRawMutex, ThreadModeRawMutex};
use embassy_sync::channel::Channel;
use static_cell::StaticCell;

use crate::core;

pub struct ToolBoard<'a> {
    pub driver_diag0: Input<'a>,
    pub driver_diag1: Input<'a>,
    pub driver_refr: Input<'a>,
    pub driver_refl: Input<'a>,
    pub driver_en: Output<'a>,

    pub actuator_limit_1: Input<'a>,
    pub actuator_limit_2: Input<'a>,

    pub side_break_beam_1: Input<'a>,
    pub side_break_beam_2: Input<'a>,
}

bind_interrupts!(struct Irqs {
    FDCAN1_IT0 => can::IT0InterruptHandler<peripherals::FDCAN1>;
    FDCAN1_IT1 => can::IT1InterruptHandler<peripherals::FDCAN1>;
    I2C3_EV => i2c::EventInterruptHandler<peripherals::I2C3>;
    I2C3_ER => i2c::ErrorInterruptHandler<peripherals::I2C3>;
});

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

impl<'a> ToolBoard<'a> {
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

        // Trinamic Driver Setup
        let driver_en = Output::new(p.PB6, Level::High, Speed::Low);
        let driver_refr = Input::new(p.PA0, Pull::None);
        let driver_refl = Input::new(p.PA1, Pull::None);
        let driver_diag0 = Input::new(p.PB5, Pull::None);
        let driver_diag1 = Input::new(p.PB4, Pull::None);

        // Actuators
        let actuator_limit_1 = Input::new(p.PB0, Pull::None);
        let actuator_limit_2 = Input::new(p.PB1, Pull::None);

        // Side Break Beams
        let side_break_beam_1 = Input::new(p.PB7, Pull::None);
        let side_break_beam_2 = Input::new(p.PB9, Pull::None);

        // IO I2C
        // IO I2C INT

        // let io_int = Input::new(p.PC10, Pull::None);
        // let mut i2c_config = embassy_stm32::i2c::Config::default();
        // i2c_config.scl_pullup = false;
        // i2c_config.sda_pullup = false;
        // let slot_i2c = embassy_stm32::i2c::I2c::new(
        //     p.I2C3,
        //     p.PC8,
        //     p.PC9,
        //     Irqs,
        //     p.DMA2_CH1,
        //     p.DMA2_CH2,
        //     embassy_stm32::time::Hertz(100_000),
        //     i2c_config
        // );

        // Carriage CS
        // Puller CS
        // IO SPI
        // let puller_cs = Output::new(p.PB10, Level::High, Speed::Low);
        // let carriage_cs = Output::new(p.PB11, Level::High, Speed::Low);
        // let driver_cs = Output::new(p.PD2, Level::High, Speed::Low);
        // let spi_config = embassy_stm32::spi::Config::default();
        // let io_spi = embassy_stm32::spi::Spi::new(
        //     p.SPI2,
        //     p.PB13,
        //     p.PB15,
        //     p.PB14,
        //     p.DMA1_CH4,
        //     p.DMA1_CH5,
        //     spi_config
        // );

        // EEPROM CS
        // EEPROM SPI
        // let puller_cs = Output::new(p.PA15, Level::High, Speed::Low);
        // let spi_config = embassy_stm32::spi::Config::default();
        // let eeprom_spi = embassy_stm32::spi::Spi::new(
        //     p.SPI1,
        //     p.PA5,
        //     p.PA7,
        //     p.PA6,
        //     p.DMA1_CH6,
        //     p.DMA1_CH7,
        //     spi_config
        // );

        Self {
            driver_diag0,
            driver_diag1,
            driver_refr,
            driver_refl,
            driver_en,
            actuator_limit_1,
            actuator_limit_2,
            side_break_beam_1,
            side_break_beam_2,
        }
    }

    pub async fn run(&mut self) -> ! {
        loop {
            //let command = COMMAND_CHANNEL.receive().await;
            //if let Some(cmd) = parse_relay_command(&command) {
            //    let response = self.handle_command(cmd);
            //    self.set_bank_1();
            //    self.set_bank_2();
            //    RESPONSE_CHANNEL.send(response.serialize_to_frame()).await;
            //} else {
            //    RESPONSE_CHANNEL
            //        .send(RelayResponses::Invalid.serialize_to_frame())
            //        .await;
            //}
        }
    }
}
