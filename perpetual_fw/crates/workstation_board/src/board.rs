use embassy_executor::Spawner;
use embassy_stm32::can::filter::StandardFilter;
use embassy_stm32::can::{enums::BusError, frame::Envelope, Frame};
use embassy_stm32::gpio::{Level, Output, Speed};
use embassy_stm32::mode::Async;
use embassy_stm32::usart::Uart;
use embassy_stm32::Config;
use embassy_stm32::{bind_interrupts, can, peripherals};
use embassy_sync::blocking_mutex::raw::{CriticalSectionRawMutex, ThreadModeRawMutex};
use embassy_sync::channel::Channel;
//use embedded_can::StandardId;
use modbus_core::rtu::client::{decode_response, encode_request};
use modbus_core::rtu::RequestAdu;
use modbus_core::Response;
use static_cell::StaticCell;
use waveshare_digital::relay::RelayController;
use waveshare_digital::Transport;

use crate::core::{BoardState, Messages, Responses};

pub struct IoBoard<'a> {
    pub relays: u8,
    pub led: Output<'a>,
    pub rc: RelayController<UartWrapper>,
}

bind_interrupts!(struct Irqs {
    FDCAN1_IT0 => can::IT0InterruptHandler<peripherals::FDCAN1>;
    FDCAN1_IT1 => can::IT1InterruptHandler<peripherals::FDCAN1>;
    USART1 => embassy_stm32::usart::InterruptHandler<peripherals::USART1>;
});

static COMMAND_CHANNEL: Channel<ThreadModeRawMutex, Messages, 64> = Channel::new();
static RESPONSE_CHANNEL: Channel<ThreadModeRawMutex, Responses, 64> = Channel::new();

pub struct UartWrapper(embassy_stm32::usart::Uart<'static, Async>);

pub enum ModbusDriverError<E> {
    TransportError(E),
    ProtocolError(modbus_core::Error),
    ModbusException(modbus_core::ExceptionResponse),
    Unknown,
}

impl Transport for UartWrapper {
    type Error = ModbusDriverError<embassy_stm32::usart::Error>;
    async fn execute_transaction<'a>(
        &'a mut self,
        request: RequestAdu<'_>,
        tx_buffer: &mut [u8],
        rx_buffer: &'a mut [u8],
    ) -> Result<Response<'a>, Self::Error> {
        let size = encode_request(request, tx_buffer).unwrap();
        self.0
            .write(&tx_buffer[0..size])
            .await
            .map_err(ModbusDriverError::TransportError)?;
        //let _ = embassy_time::Timer::after_millis(1).await;
        let mut n = 0;
        loop {
            match embassy_time::with_timeout(
                embassy_time::Duration::from_millis(100),
                self.0.read(&mut rx_buffer[n..n + 1]),
            )
            .await
            {
                Ok(result) => {
                    n += 1;
                    if n > 255 {
                        break;
                    }
                }
                Err(_) => break,
            }
        }
        //let n = self
        //    .0
        //    .read_until_idle(rx_buffer)
        //    .await
        //    .map_err(ModbusDriverError::TransportError)?;
        defmt::info!("TX {:?}", &tx_buffer[0..size]);
        defmt::info!("RX {:?}", &rx_buffer[0..n]);
        match decode_response(&rx_buffer[0..n]) {
            Ok(Some(response)) => match response.pdu.0 {
                Ok(pdu) => Ok(pdu),
                Err(e) => Err(ModbusDriverError::ModbusException(e)),
            },
            Ok(None) => Err(ModbusDriverError::Unknown),
            Err(e) => Err(ModbusDriverError::ProtocolError(e)),
        }
    }
}

//#[embassy_executor::task]
//async fn workstation_monitor(
//    input_6: embassy_stm32::gpio::Input<'static>,
//) -> ! {
//}

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
            //else {
            //    match e.frame.id() {
            //        embedded_can::Id::Standard(s) => defmt::info!("{}", s.as_raw()),
            //        _ => (),
            //    }
            //}
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
        let _ = embassy_time::Timer::after_millis(100).await;
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
        let filter: StandardFilter = embassy_stm32::can::filter::Filter {
            filter: can::filter::FilterType::DedicatedSingle(
                embedded_can::StandardId::new(200).unwrap(),
            ),
            action: embassy_stm32::can::filter::Action::StoreInFifo0,
        };
        can.properties()
            .set_standard_filter(can::filter::StandardFilterSlot::_0, filter);

        can.config().set_automatic_retransmit(true);
        can.config()
            .set_global_filter(embassy_stm32::can::config::GlobalFilter {
                handle_standard_frames: can::config::NonMatchingFilter::Reject,
                handle_extended_frames: can::config::NonMatchingFilter::Reject,
                reject_remote_standard_frames: false,
                reject_remote_extended_frames: false,
            });
        for slot_num in 1..28 {
            let f = can::filter::StandardFilter::reject_all();
            can.properties().set_standard_filter(
                embassy_stm32::can::filter::StandardFilterSlot::from(slot_num as u8),
                f,
            );
        }

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
        let led = embassy_stm32::gpio::Output::new(p.PA5, Level::High, Speed::High);
        let mut uart_config = embassy_stm32::usart::Config::default();
        uart_config.baudrate = 57600;
        let uart = Uart::new(
            p.USART1,
            p.PC5,
            p.PC4,
            Irqs,
            p.DMA1_CH7,
            p.DMA1_CH8,
            uart_config,
        )
        .unwrap();

        let rc = RelayController::new(1, UartWrapper(uart));

        Self { relays: 0, led, rc }
    }

    fn get_state(&mut self) -> BoardState {
        BoardState {
            relays: self.relays,
        }
    }

    pub async fn handle_message(&mut self, msg: Messages) -> Responses {
        match msg {
            Messages::GetState => Responses::State(self.get_state()),
            Messages::SetRelays(state) => {
                self.relays = state;
                for i in 0..4 {
                    if state & (0x01 >> i) > 0 {
                        let _ = self
                            .rc
                            .close_io_channel(
                                waveshare_digital::relay::Channel::try_from(i).unwrap(),
                            )
                            .await;
                    } else {
                        let _ = self
                            .rc
                            .open_io_channel(
                                waveshare_digital::relay::Channel::try_from(i).unwrap(),
                            )
                            .await;
                    }
                }
                Responses::State(self.get_state())
            }
        }
    }

    pub async fn run(&mut self) -> ! {
        loop {
            //klet cmd = COMMAND_CHANNEL.receive().await;
            //klet response = self.handle_message(cmd);
            //kRESPONSE_CHANNEL.send(response).await;
            self.led.toggle();
            defmt::info!("Start Relays");
            //let _ = self
            //    .rc
            //    .open_io_channel(waveshare_digital::relay::Channel::Channel0)
            //    .await;
            let _ = embassy_time::Timer::after_millis(2000).await;
            let _ = self
                .rc
                .close_io_channel(waveshare_digital::relay::Channel::Channel0)
                .await;
            let _ = embassy_time::Timer::after_millis(2000).await;
            /*
            let _ = self
                .rc
                .open_io_channel(waveshare_digital::relay::Channel::Channel1)
                .await;

            let _ = self
                .rc
                .open_io_channel(waveshare_digital::relay::Channel::Channel2)
                .await;
            let _ = self
                .rc
                .open_io_channel(waveshare_digital::relay::Channel::Channel3)
                .await;
            let _ = self
                .rc
                .open_io_channel(waveshare_digital::relay::Channel::Channel4)
                .await;
            let _ = embassy_time::Timer::after_millis(2000).await;
            let _ = self
                .rc
                .close_io_channel(waveshare_digital::relay::Channel::Channel1)
                .await;
            let _ = self
                .rc
                .close_io_channel(waveshare_digital::relay::Channel::Channel2)
                .await;
            let _ = self
                .rc
                .close_io_channel(waveshare_digital::relay::Channel::Channel3)
                .await;
            let _ = self
                .rc
                .close_io_channel(waveshare_digital::relay::Channel::Channel4)
                .await;
            */
        }
    }
}
