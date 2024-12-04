#![no_std]

pub mod registers;

use bitfield::bitfield;
use embedded_hal::digital::{InputPin, OutputPin};
use embedded_hal_async::spi::SpiDevice;
use registers::Registers;

pub struct TMC5130<SPI, EN, DIAG0, DIAG1> {
    spi: SPI,
    enable: EN,
    diag0: DIAG0,
    diag1: DIAG1,
}

const WRITE_MASK: u8 = 0x80;

/*
Driver Status:
7 status_stop_r RAMP_STAT[1] – 1: Signals stop right switch status (motion controller
only)
6 status_stop_l RAMP_STAT[0] – 1: Signals stop left switch status (motion controller only)
5 position_reached RAMP_STAT[9] – 1: Signals target reached (motion controller only)
4 velocity_reached RAMP_STAT[8] – 1: Signals target velocity reached (motion controller only)
3 standstill DRV_STATUS[31] – 1: Signals motor stand still
2 sg2 DRV_STATUS[24] – 1: Signals StallGuard flag active
1 driver_error GSTAT[1] – 1: Signals driver 1 driver error (clear by reading GSTAT)
0 reset_flag GSTAT[0] – 1: Signals, that a reset has occurred (clear by reading GSTAT)
*/

bitfield! {
    pub struct TmcStatus(u8);
    impl Debug;
    pub stop_left, _: 7;
    pub stop_right, _: 6;
    pub position_reached, _: 5;
    pub velocity_reached, _: 4;
    pub standstill, _: 4;
    pub sg2, _: 4;
    pub driver_error, _: 4;
    pub reset_flag, _: 4;
}

impl<SPI, EN, DIAG0, DIAG1, E> TMC5130<SPI, EN, DIAG0, DIAG1>
where
    SPI: SpiDevice<Error = E>,
    EN: OutputPin,
    DIAG0: InputPin,
    DIAG1: InputPin,
{
    pub fn new(spi: SPI, enable: EN, diag0: DIAG0, diag1: DIAG1) -> Self {
        Self {
            spi,
            //cs,
            enable,
            diag0,
            diag1,
        }
    }

    pub fn set_en(&mut self) {
        let _ = self.enable.set_high();
    }

    pub fn clear_en(&mut self) {
        let _ = self.enable.set_low();
    }

    pub fn diag0(&mut self) -> bool {
        if let Ok(state) = self.diag0.is_high() {
            return state;
        }
        panic!("Unable to Read");
    }

    pub fn diag1(&mut self) -> bool {
        if let Ok(state) = self.diag1.is_high() {
            return state;
        }
        panic!("Unable to Read");
    }

    pub async fn transfer_data(
        &mut self,
        rx_buffer: &mut [u8],
        tx_buffer: &mut [u8],
    ) -> Result<TmcStatus, E> {
        self.spi.transfer(rx_buffer, tx_buffer).await?;
        Ok(TmcStatus(rx_buffer[0]))
    }

    pub async fn read_register(&mut self, register: Registers) -> Result<[u8; 5], E> {
        let mut tx_buffer = [0u8; 5];
        let mut rx_buffer = [0u8; 5];
        tx_buffer[0] = register.addr();
        let _ = self.transfer_data(&mut rx_buffer, &mut tx_buffer).await;
        Ok(rx_buffer)
    }

    pub async fn write_register(
        &mut self,
        register: Registers,
        data: [u8; 4],
    ) -> Result<[u8; 5], E> {
        let mut tx_buffer = [0u8; 5];
        let mut rx_buffer = [0u8; 5];
        tx_buffer[0] = register.addr() | WRITE_MASK;
        tx_buffer[1] = data[0];
        tx_buffer[2] = data[1];
        tx_buffer[3] = data[2];
        tx_buffer[4] = data[3];
        let _ = self.transfer_data(&mut rx_buffer, &mut tx_buffer).await;
        Ok(rx_buffer)
    }

    pub async fn write_i32(&mut self, register: Registers, data: i32) -> Result<[u8; 5], E> {
        let mut tx_buffer = [0u8; 5];
        let mut rx_buffer = [0u8; 5];
        tx_buffer[0] = register.addr() | WRITE_MASK;
        let bytes = data.to_le_bytes();
        tx_buffer[1] = bytes[0];
        tx_buffer[2] = bytes[1];
        tx_buffer[3] = bytes[2];
        tx_buffer[4] = bytes[3];
        let _ = self.transfer_data(&mut rx_buffer, &mut tx_buffer).await;
        Ok(rx_buffer)
    }

    pub async fn write_u32(&mut self, register: Registers, data: u32) -> Result<[u8; 5], E> {
        let mut tx_buffer = [0u8; 5];
        let mut rx_buffer = [0u8; 5];
        tx_buffer[0] = register.addr() | WRITE_MASK;
        let bytes = data.to_le_bytes();
        tx_buffer[1] = bytes[0];
        tx_buffer[2] = bytes[1];
        tx_buffer[3] = bytes[2];
        tx_buffer[4] = bytes[3];
        let _ = self.transfer_data(&mut rx_buffer, &mut tx_buffer).await;
        Ok(rx_buffer)
    }

    pub async fn read_i32(&mut self, register: Registers) -> Result<i32, E> {
        let mut tx_buffer = [0u8; 5];
        let mut rx_buffer = [0u8; 5];
        tx_buffer[0] = register.addr();
        let _ = self.transfer_data(&mut rx_buffer, &mut tx_buffer).await;
        Ok(i32::from_le_bytes(rx_buffer[1..5].try_into().unwrap()))
    }

    pub async fn read_u32(&mut self, register: Registers) -> Result<u32, E> {
        let mut tx_buffer = [0u8; 5];
        let mut rx_buffer = [0u8; 5];
        tx_buffer[0] = register.addr();
        let _ = self.transfer_data(&mut rx_buffer, &mut tx_buffer).await;
        Ok(u32::from_le_bytes(rx_buffer[1..5].try_into().unwrap()))
    }
}

