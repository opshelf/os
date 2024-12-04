#![no_std]

use bitfield::bitfield;
use embedded_hal::digital::OutputPin;
use embedded_hal_async::spi::SpiDevice;
use paste::paste;

/// Registers
#[derive(Debug, Copy, Clone, PartialEq, Eq)]
#[repr(u8)]
pub enum Registers {
    IODIR = 0x00,
    IPOL = 0x01,
    GPINTEN = 0x02,
    DEFVAL = 0x03,
    INTCON = 0x04,
    IOCON = 0x05,
    GPPU = 0x06,
    INTF = 0x07,
    INTCAP = 0x08,
    GPIO = 0x09,
    OLAT = 0x0A,
}

impl From<u8> for Registers {
    fn from(value: u8) -> Self {
        match value {
            0x00 => Registers::IODIR,
            0x01 => Registers::IPOL,
            0x02 => Registers::GPINTEN,
            0x03 => Registers::DEFVAL,
            0x04 => Registers::INTCON,
            0x05 => Registers::IOCON,
            0x06 => Registers::GPPU,
            0x07 => Registers::INTF,
            0x08 => Registers::INTCAP,
            0x09 => Registers::GPIO,
            0x0A => Registers::OLAT,
            _ => panic!("Invalid Register"),
        }
    }
}

#[derive(Debug, Copy, Clone, PartialEq, Eq, Default)]
#[repr(u8)]
pub enum Pins {
    #[default]
    P0 = 0x00,
    P1 = 0x01,
    P2 = 0x02,
    P3 = 0x03,
    P4 = 0x04,
    P5 = 0x05,
    P6 = 0x06,
    P7 = 0x07,
}

///OP CODES
#[derive(Debug, Copy, Clone, PartialEq, Eq)]
#[repr(u8)]
pub enum OpCodes {
    READ = 0x41,
    WRITE = 0x40,
}

/// Pin modes.
#[derive(Debug, Copy, Clone, PartialEq, Eq)]
#[repr(u8)]
pub enum Direction {
    /// Input mode.
    Input = 1,
    /// Output mode.
    Output = 0,
}

/// Pin levels.
#[derive(Debug, Copy, Clone, PartialEq, Eq)]
#[repr(u8)]
pub enum Level {
    /// High level
    High = 1,
    /// Low level
    Low = 0,
}

/// Pin Pull Up state.
#[derive(Debug, Copy, Clone, PartialEq, Eq)]
#[repr(u8)]
pub enum PullUp {
    /// Weak pull up enabled
    Enabled = 1,
    /// Weak pull up disabled, pin floating
    Disabled = 0,
}

/// Interrupt on change state.
#[derive(Debug, Copy, Clone, PartialEq, Eq)]
#[repr(u8)]
pub enum InterruptOnChange {
    /// Enabled
    Enabled = 1,
    /// Disables
    Disabled = 0,
}

/// Interrupt control.
#[derive(Debug, Copy, Clone, PartialEq, Eq)]
#[repr(u8)]
pub enum IntMode {
    /// Interrupt on level (seel DEFVAL, )
    OnLevel = 1,
    /// Interrupt on change (see GPINTEN, )
    OnChange = 0,
}

/// Interrupt flag.
#[derive(Debug, Copy, Clone, PartialEq, Eq)]
#[repr(u8)]
pub enum IntFlag {
    /// Interrupt asserted
    Asserted = 1,
    /// Interrupt not asserted
    Deasserted = 0,
}

/// Pin Input polarity inversion.
#[derive(Debug, Copy, Clone, PartialEq, Eq)]
#[repr(u8)]
pub enum Polarity {
    /// Inverted input polarity
    Inverted = 1,
    /// Not inverted
    NotInverted = 0,
}

bitfield! {
    pub struct IOCON(u8);
    impl Debug;
    pub seqop, set_seqop: 5;
    pub disslw, set_disslw: 4;
    pub haen, set_haen: 3;
    pub odr, set_odr: 2;
    pub intpol, set_intpol: 1;
}

macro_rules! reg_reader_setter {
    ($name:ident, $reg:ident, $repr:ident) => {
        paste! {
            pub async fn [<set_ $name>](&mut self, value: u8) -> Result<(), E> {
                self.write_register(Registers::$reg as u8, value).await
            }

            pub async fn [<read_ $name>](&mut self) -> Result<u8, E> {
                self.read_register(Registers::$reg as u8).await
            }

            pub async fn [<set_ $name _bit>](&mut self, pin: Pins, value: $repr) -> Result<u8, E> {
                self.set_bit(Registers::$reg as u8, pin as u8, (value as u8) != 0).await
            }

            pub async fn [<get_ $name _bit>](&mut self, pin: Pins) -> Result<bool, E> {
                //let result = self.get_bit(Registers::$reg as u8, pin as u8).await?;
                //Ok(result)
                self.get_bit(Registers::$reg as u8, pin as u8).await
            }
        }
    };
    ($name:ident, $reg:ident) => {
        paste! {
            pub async fn [<set_ $name>](&mut self, value: u8) -> Result<(), E> {
                self.write_register(Registers::$reg as u8, value).await
            }

            pub async fn [<read_ $name>](&mut self) -> Result<u8, E> {
                self.read_register(Registers::$reg as u8).await
            }
        }
    };
}

//pub struct MCP23S08<SPI, CS> {
//    pub spi: SPI,
//    pub cs: CS,
//}
pub struct MCP23S08<SPI> {
    pub spi: SPI,
}

//impl<SPI, CS, E> MCP23S08<SPI, CS>
impl<SPI, E> MCP23S08<SPI>
where
    SPI: SpiDevice<Error = E>,
    //CS: OutputPin,
{
    // pub fn new(spi: SPI, mut cs: CS) -> Self {
    //     let _ = cs.set_high();
    //     Self { spi, cs }
    // }
    pub fn new(spi: SPI) -> Self {
        Self { spi }
    }

    pub async fn read_register(&mut self, reg: u8) -> Result<u8, E> {
        //let _ = self.cs.set_low();
        let mut in_buffer: [u8; 3] = [0; 3];
        let out_buffer: [u8; 3] = [OpCodes::READ as u8, reg, 0x00];
        self.spi.transfer(&mut in_buffer, &out_buffer).await?;
        //let _ = self.cs.set_high();
        return Ok(in_buffer[2]);
    }

    pub async fn write_register(&mut self, reg: u8, value: u8) -> Result<(), E> {
        //let _ = self.cs.set_low();
        let mut in_buffer: [u8; 3] = [0; 3];
        let out_buffer: [u8; 3] = [OpCodes::WRITE as u8, reg, value];
        self.spi.transfer(&mut in_buffer, &out_buffer).await?;
        //let _ = self.cs.set_high();
        Ok(())
    }

    pub async fn mask_register(&mut self, reg: u8, value: u8) -> Result<u8, E> {
        let current_value = self.read_register(reg).await?;
        let _ = self.write_register(reg, current_value & value).await?;
        Ok(current_value & value)
    }

    pub async fn xor_register(&mut self, reg: u8, value: u8) -> Result<u8, E> {
        let current_value = self.read_register(reg).await?;
        let _ = self.write_register(reg, current_value | value).await?;
        Ok(current_value | value)
    }

    pub async fn set_bit(&mut self, reg: u8, bit: u8, value: bool) -> Result<u8, E> {
        self.xor_register(reg, (value as u8) << bit).await
    }

    pub async fn get_bit(&mut self, reg: u8, bit: u8) -> Result<bool, E> {
        let result = self.read_register(reg).await?;
        return Ok((result & (0x01 << bit)) > 0);
    }

    pub async fn set_all_directions(&mut self, direction: Direction) -> Result<(), E> {
        if direction == Direction::Output {
            self.write_register(Registers::IODIR as u8, 0x00).await
        } else {
            self.write_register(Registers::IODIR as u8, 0xFF).await
        }
    }

    pub async fn set_iocon(&mut self, iocon: IOCON) -> Result<(), E> {
        self.write_register(Registers::IOCON as u8, iocon.0).await
    }

    pub async fn read_iocon(&mut self) -> Result<IOCON, E> {
        let value = self.read_register(Registers::IOCON as u8).await?;
        Ok(IOCON(value))
    }

    reg_reader_setter!(iodir, IODIR, Direction);
    reg_reader_setter!(ipol, IPOL, Polarity);
    reg_reader_setter!(gpinten, GPINTEN, InterruptOnChange);
    reg_reader_setter!(defval, DEFVAL);
    reg_reader_setter!(intcon, INTCON, IntMode);
    reg_reader_setter!(gppu, GPPU, PullUp);
    reg_reader_setter!(intf, INTF, IntFlag);
    reg_reader_setter!(intcap, INTCAP, IntFlag);
    reg_reader_setter!(gpio, GPIO, Level);
    reg_reader_setter!(olat, OLAT, Level);
}

