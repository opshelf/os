use embassy_embedded_hal::shared_bus::asynch::spi::SpiDevice;
use embassy_executor::Spawner;
use embassy_stm32::can::{enums::BusError, frame::Envelope, Frame};
use embassy_stm32::gpio::{Input, Level, Output, Pull, Speed};
use embassy_stm32::mode::Async;
use embassy_stm32::spi::MODE_3;
use embassy_stm32::time::Hertz;
use embassy_stm32::Config;
use embassy_stm32::{bind_interrupts, can, i2c, peripherals};
use embassy_sync::blocking_mutex::raw::{CriticalSectionRawMutex, ThreadModeRawMutex};
use embassy_sync::channel::Channel;
use embassy_sync::mutex::Mutex;
use mcp23s08::MCP23S08;
use static_cell::StaticCell;
use tca9535::{Port, TCA9535};
use tmc5130::TMC5130;

use crate::core::{
    BoardState, CarriageState, InputState, Inputs, Messages, PullerState, Responses, SlotStatus,
};

type IoI2C<'a> = embassy_stm32::i2c::I2c<'a, Async>;

pub struct ToolBoard<'a> {
    //pub driver_diag0: Input<'a>,
    //pub driver_diag1: Input<'a>,
    pub driver_refr: Input<'a>,
    pub driver_refl: Input<'a>,
    //pub driver_en: Output<'a>,
    pub actuator_limit_1: Input<'a>,
    pub actuator_limit_2: Input<'a>,

    pub side_break_beam_1: Input<'a>,
    pub side_break_beam_2: Input<'a>,

    pub io: TCA9535<IoI2C<'a>>,
    pub puller: MCP23S08<
        SpiDevice<'a, CriticalSectionRawMutex, embassy_stm32::spi::Spi<'static, Async>, Output<'a>>,
    >,
    pub carriage: MCP23S08<
        SpiDevice<'a, CriticalSectionRawMutex, embassy_stm32::spi::Spi<'static, Async>, Output<'a>>,
    >,
    pub trinamic: TMC5130<
        SpiDevice<'a, CriticalSectionRawMutex, embassy_stm32::spi::Spi<'static, Async>, Output<'a>>,
        Output<'a>,
        Input<'a>,
        Input<'a>,
    >,
    puller_state: u8,
    carriage_state: u8,
}

bind_interrupts!(struct Irqs {
    FDCAN1_IT0 => can::IT0InterruptHandler<peripherals::FDCAN1>;
    FDCAN1_IT1 => can::IT1InterruptHandler<peripherals::FDCAN1>;
    I2C3_EV => i2c::EventInterruptHandler<peripherals::I2C3>;
    I2C3_ER => i2c::ErrorInterruptHandler<peripherals::I2C3>;
});

//static COMMAND_CHANNEL: Channel<ThreadModeRawMutex, [u8; 8], 64> = Channel::new();
//static RESPONSE_CHANNEL: Channel<ThreadModeRawMutex, [u8; 8], 64> = Channel::new();

static COMMAND_CHANNEL: Channel<ThreadModeRawMutex, Messages, 64> = Channel::new();
static RESPONSE_CHANNEL: Channel<ThreadModeRawMutex, Responses, 64> = Channel::new();

static SPI_CELL: StaticCell<Mutex<CriticalSectionRawMutex, embassy_stm32::spi::Spi<'_, Async>>> =
    StaticCell::new();

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
        let mut i2c_config = embassy_stm32::i2c::Config::default();
        i2c_config.scl_pullup = false;
        i2c_config.sda_pullup = false;
        let slot_i2c = embassy_stm32::i2c::I2c::new(
            p.I2C3,
            p.PC8,
            p.PC9,
            Irqs,
            p.DMA2_CH1,
            p.DMA2_CH2,
            embassy_stm32::time::Hertz(100_000),
            i2c_config,
        );

        let puller_cs = Output::new(p.PB10, Level::High, Speed::Low);
        let carriage_cs = Output::new(p.PB11, Level::High, Speed::Low);
        let trinamic_cs = Output::new(p.PD2, Level::High, Speed::Low);
        let mut spi_2_config = embassy_stm32::spi::Config::default();
        spi_2_config.mode = MODE_3;
        spi_2_config.frequency = Hertz(500_000);
        let spi_2 = embassy_stm32::spi::Spi::new(
            p.SPI2,
            p.PB13,
            p.PB15,
            p.PB14,
            p.DMA1_CH4,
            p.DMA1_CH5,
            spi_2_config,
        );

        let spi_bus: Mutex<CriticalSectionRawMutex, embassy_stm32::spi::Spi<'_, Async>> =
            Mutex::new(spi_2);
        let spi_bus = SPI_CELL.init(spi_bus);
        let spi_device = SpiDevice::new(spi_bus, puller_cs);
        let puller = MCP23S08::new(spi_device);
        let spi_device = SpiDevice::new(spi_bus, carriage_cs);
        let carriage = MCP23S08::new(spi_device);
        let spi_device = SpiDevice::new(spi_bus, trinamic_cs);
        let tmc5130 = TMC5130::new(spi_device, driver_en, driver_diag0, driver_diag1);
        // EEPROM CS
        // TRINAMIC CS
        // EEPROM SPI
        //let mut eeprom_cs = Output::new(p.PA15, Level::High, Speed::Low);
        //eeprom_cs.set_high();
        //let mut spi_2_config = embassy_stm32::spi::Config::default();
        //spi_2_config.mode = MODE_3;
        //spi_2_config.frequency = Hertz(250_000);
        //let spi_2 = embassy_stm32::spi::Spi::new(
        //    p.SPI1,
        //    p.PA5,
        //    p.PA7,
        //    p.PA6,
        //    p.DMA1_CH6,
        //    p.DMA1_CH7,
        //    spi_2_config,
        //);
        //tmc5130.clear_en();

        Self {
            driver_refr,
            driver_refl,
            actuator_limit_1,
            actuator_limit_2,
            side_break_beam_1,
            side_break_beam_2,
            io: TCA9535::new(slot_i2c, 0x20),
            puller,
            trinamic: tmc5130,
            carriage,
            puller_state: 0,
            carriage_state: 0,
        }
    }

    async fn get_slot_state(&mut self) -> SlotStatus {
        let io_status = self
            .io
            .read_port(tca9535::registers::Registers::INPUT_PORT0)
            .await
            .unwrap();
        SlotStatus(io_status.bits().to_le_bytes()[0])
    }

    fn get_inputs(&mut self) -> Inputs {
        let right_presence = self.driver_refr.is_high();
        let left_presence = self.driver_refl.is_high();
        let forward_actuator = self.actuator_limit_1.is_high();
        let backward_actuator = self.actuator_limit_2.is_high();
        let left_beam = self.side_break_beam_1.is_high();
        let right_beam = self.side_break_beam_2.is_high();
        let mut inputs = Inputs(0);

        if right_presence {
            inputs.set_right_presence(true)
        };
        if left_presence {
            inputs.set_left_presence(true)
        };
        if forward_actuator {
            inputs.set_forward_actuator(true)
        };
        if backward_actuator {
            inputs.set_backward_actuator(true)
        };
        if left_beam {
            inputs.set_left_beam(true)
        };
        if right_beam {
            inputs.set_right_beam(true)
        };
        inputs
    }

    pub async fn get_input_state(&mut self) -> InputState {
        let inputs = self.get_inputs();
        let slots = self.get_slot_state().await;
        InputState { inputs, slots }
    }

    pub async fn get_state(&mut self) -> BoardState {
        let inputs = self.get_input_state().await;
        BoardState {
            inputs,
            carriage: CarriageState(self.carriage_state),
            puller: PullerState(self.puller_state),
        }
    }

    pub async fn set_pullers(&mut self) {
        let _ = self.puller.set_gpio(self.puller_state).await;
        let _ = self.puller.set_olat(self.puller_state).await;
    }

    pub async fn set_carriage(&mut self) {
        let _ = self.carriage.set_gpio(self.carriage_state).await;
        let _ = self.carriage.set_olat(self.carriage_state).await;
    }

    pub async fn run(&mut self) -> ! {
        let config = Port::from_bits(0xFFFF).unwrap();
        let _ = self.io.write_config(config).await;
        //let mut pos: i32 = 1000;
        //let res = self
        //    .trinamic
        //    .write_u32(tmc5130::registers::Registers::V1, 10000)
        //    .await;
        //let res = self
        //    .trinamic
        //    .write_u32(tmc5130::registers::Registers::A1, 1000)
        //    .await;
        //let res = self
        //    .trinamic
        //    .write_u32(tmc5130::registers::Registers::D1, 1000)
        //    .await;
        let _ = self
            .puller
            .set_all_directions(mcp23s08::Direction::Output)
            .await;
        let result = self.puller.read_iodir().await;
        defmt::info!("IODIR {:?}", result);
        let result = self.puller.read_ipol().await;
        defmt::info!("IPOL {:?}", result);

        let _ = self
            .carriage
            .set_all_directions(mcp23s08::Direction::Output)
            .await;
        let result = self.carriage.read_iodir().await;
        defmt::info!("IODIR {:?}", result);
        let result = self.carriage.read_ipol().await;
        defmt::info!("IPOL {:?}", result);

        loop {
            let cmd = COMMAND_CHANNEL.receive().await;
            match cmd {
                Messages::GetState => {
                    let board_state = self.get_state().await;
                    RESPONSE_CHANNEL.send(Responses::State(board_state)).await;
                }
                Messages::PullerCommand(cmd) => {
                    match cmd {
                        crate::core::PullerCommands::SetPullers(value) => {
                            self.puller_state = value.0;
                            self.set_pullers().await;
                        }
                        crate::core::PullerCommands::MaskPullers(value) => {
                            self.puller_state |= value.0;
                            self.set_pullers().await;
                        }
                    };
                    let board_state = self.get_state().await;
                    RESPONSE_CHANNEL.send(Responses::State(board_state)).await;
                }
                Messages::CarriageCommand(cmd) => {
                    match cmd {
                        crate::core::CarriageCommands::SetCarriage(value) => {
                            self.carriage_state = value.0;
                            self.set_pullers().await;
                        }
                        crate::core::CarriageCommands::MaskCarriage(value) => {
                            self.carriage_state |= value.0;
                            self.set_pullers().await;
                        }
                    };
                    let board_state = self.get_state().await;
                    RESPONSE_CHANNEL.send(Responses::State(board_state)).await;
                }
            };
            //defmt::info!("Running");
            ////let _result = self.puller.set_gpio(255).await;
            ////let _result = self.puller.set_olat(255).await;
            //let result = self.carriage.set_gpio(255).await;
            //defmt::info!("{:?}", result);
            //let result = self.carriage.set_olat(255).await;
            //defmt::info!("{:?}", result);
            //embassy_time::Timer::after_millis(1000).await;
            ////let _result = self.puller.set_gpio(0).await;
            ////let _result = self.puller.set_olat(0).await;
            //let result = self.carriage.set_gpio(0).await;
            //defmt::info!("{:?}", result);
            //let result = self.carriage.set_olat(0).await;
            //defmt::info!("{:?}", result);
            //embassy_time::Timer::after_millis(1000).await;
            /*
            defmt::info!(
                "{} {} {} {} {} {}",
                self.driver_refr.is_high(),
                self.driver_refl.is_high(),
                self.actuator_limit_1.is_high(),
                self.actuator_limit_2.is_high(),
                self.side_break_beam_1.is_high(),
                self.side_break_beam_2.is_high(),
            );
            let io_status = self
                .io
                .read_port(tca9535::registers::Registers::INPUT_PORT0)
                .await;
            let status = io_status.unwrap_or(Port::from_bits(0xAAFF).unwrap());
            defmt::info!("{:?}", status.bits().to_be_bytes());
            */
            /*
            let res = self
                .trinamic
                .read_register(tmc5130::registers::Registers::X_ENC)
                .await;
            defmt::info!("X ENC: {:?}", res);
            let res = self
                .trinamic
                .read_register(tmc5130::registers::Registers::GSTAT)
                .await;
            defmt::info!("GSTAT: {:?}", res);
            let res = self
                .trinamic
                .read_register(tmc5130::registers::Registers::GCONF)
                .await;
            defmt::info!("GCONF: {:?}", res);
            let res = self
                .trinamic
                .read_register(tmc5130::registers::Registers::IHOLD_IRUN)
                .await;
            defmt::info!("IHOLD_IRUN: {:?}", res);
            if let Ok(res) = self
                .trinamic
                .read_i32(tmc5130::registers::Registers::X_COMPARE)
                .await
            {
                defmt::info!("Position Reached: {:?}", res);
                if res == 0 {
                    self.trinamic
                        .write_i32(tmc5130::registers::Registers::XTARGET, pos)
                        .await;
                    pos += 1000;
                }
            }
            */
            //embassy_time::Timer::after_millis(1000).await;
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
