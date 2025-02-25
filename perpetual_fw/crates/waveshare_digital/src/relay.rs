use core::u16;

use crate::Transport;
//use anyhow::Result;
#[cfg(feature = "defmt")]
use defmt;
use modbus_core::rtu::{Header, RequestAdu};
use modbus_core::{Coils, Request, RequestPdu};

#[derive(Debug, Copy, Clone)]
#[cfg_attr(feature = "defmt", derive(Format))]
pub struct RelayModule {
    pub unit_id: u8,
}

#[derive(Debug, Copy, Clone)]
#[cfg_attr(feature = "defmt", derive(Format))]
pub struct IoBank {
    pub ch0: bool,
    pub ch1: bool,
    pub ch2: bool,
    pub ch3: bool,
    pub ch4: bool,
    pub ch5: bool,
    pub ch6: bool,
    pub ch7: bool,
}

impl IoBank {
    pub fn as_action_array(&self) -> [Action; 8] {
        [
            if self.ch0 { Action::On } else { Action::Off },
            if self.ch1 { Action::On } else { Action::Off },
            if self.ch2 { Action::On } else { Action::Off },
            if self.ch3 { Action::On } else { Action::Off },
            if self.ch4 { Action::On } else { Action::Off },
            if self.ch5 { Action::On } else { Action::Off },
            if self.ch6 { Action::On } else { Action::Off },
            if self.ch7 { Action::On } else { Action::Off },
        ]
    }
}

impl From<u8> for IoBank {
    fn from(data: u8) -> Self {
        Self {
            ch0: data & 0x01 > 0,
            ch1: data & 0x02 > 0,
            ch2: data & 0x04 > 0,
            ch3: data & 0x08 > 0,
            ch4: data & 0x10 > 0,
            ch5: data & 0x20 > 0,
            ch6: data & 0x40 > 0,
            ch7: data & 0x80 > 0,
        }
    }
}

impl Into<u8> for IoBank {
    fn into(self) -> u8 {
        let mut out = 0u8;
        if self.ch0 {
            out |= 0x01
        };
        if self.ch1 {
            out |= 0x02
        };
        if self.ch2 {
            out |= 0x04
        };
        if self.ch3 {
            out |= 0x08
        };
        if self.ch4 {
            out |= 0x10
        };
        if self.ch5 {
            out |= 0x20
        };
        if self.ch6 {
            out |= 0x40
        };
        if self.ch7 {
            out |= 0x80
        };
        out
    }
}

#[derive(Debug, Copy, Clone)]
#[cfg_attr(feature = "defmt", derive(Format))]
#[repr(u16)]
pub enum OutputRegisterBases {
    OutputChannel = 0x0000,
    ControlAllRegisters = 0x00FF,
    ToggleRelays = 0x01FF,
    OutputChannelFlashOpen = 0x0200,
    OutputChannelFlashClose = 0x0400,
}

#[derive(Debug, Copy, Clone)]
#[cfg_attr(feature = "defmt", derive(Format))]
#[repr(u16)]
pub enum InputRegisterBases {
    InputChannels = 0x0000,
}

#[derive(Debug, Copy, Clone)]
#[cfg_attr(feature = "defmt", derive(Format))]
#[repr(u16)]
pub enum HoldingRegisterBases {
    ControlMode = 0x1000,
    UartParameters = 0x2000,
    DeviceAddress = 0x4000,
    SoftwareVersion = 0x8000,
}

#[derive(Debug, Copy, Clone, PartialEq, Eq)]
#[cfg_attr(feature = "defmt", derive(Format))]
#[repr(u16)]
pub enum Action {
    On = 0xFF00,
    Off = 0x0000,
    Flip = 0x5500, // Not directly supported by rmodbus
}

#[derive(Debug, Copy, Clone, PartialEq, Eq)]
#[cfg_attr(feature = "defmt", derive(Format))]
#[repr(u16)]
pub enum ControlMode {
    Command = 0x0000,
    Linked = 0x0001,
    Flip = 0x0002,
}

#[derive(Debug, Copy, Clone)]
#[cfg_attr(feature = "defmt", derive(Format))]
#[repr(u16)]
pub enum Channel {
    Channel0 = 0x0000,
    Channel1 = 0x0001,
    Channel2 = 0x0002,
    Channel3 = 0x0003,
    Channel4 = 0x0004,
    Channel5 = 0x0005,
    Channel6 = 0x0006,
    Channel7 = 0x0007,
}

impl TryFrom<u8> for Channel {
    type Error = &'static str;
    fn try_from(value: u8) -> Result<Self, Self::Error> {
        match value {
            0 => Ok(Channel::Channel0),
            1 => Ok(Channel::Channel1),
            2 => Ok(Channel::Channel2),
            3 => Ok(Channel::Channel3),
            4 => Ok(Channel::Channel4),
            5 => Ok(Channel::Channel5),
            6 => Ok(Channel::Channel6),
            7 => Ok(Channel::Channel7),
            _ => Err("Invalid Channel"),
        }
    }
}

#[derive(Debug, Copy, Clone)]
#[cfg_attr(feature = "defmt", derive(Format))]
#[repr(u16)]
pub enum Baudrates {
    B4800 = 0x00,
    B9600 = 0x01,
    B19200 = 0x02,
    B38400 = 0x03,
    B57600 = 0x04,
    B115200 = 0x05,
    B128000 = 0x06,
    B256000 = 0x07,
}

#[derive(Debug, Copy, Clone)]
#[cfg_attr(feature = "defmt", derive(Format))]
#[repr(u16)]
pub enum Parity {
    None = 0x00,
    Even = 0x01,
    Odd = 0x02,
}

impl RelayModule {
    pub fn new(unit_id: u8) -> Self {
        RelayModule { unit_id }
    }

    pub fn header(&self) -> Header {
        Header {
            slave: self.unit_id,
        }
    }

    fn create_request_adu<'a>(&self, request: Request<'a>) -> RequestAdu<'a> {
        RequestAdu {
            hdr: self.header(),
            pdu: RequestPdu(request),
        }
    }

    // TODO: ERROR ON FLIP REQUEST
    pub fn write_output_channel(&self, channel: Channel, action: Action) -> RequestAdu {
        self.create_request_adu(Request::WriteSingleCoil(
            channel as u16 + OutputRegisterBases::OutputChannel as u16,
            if action == Action::On { true } else { false },
        ))
    }

    pub fn open_all_outputs(&self) -> RequestAdu {
        self.create_request_adu(Request::WriteSingleCoil(
            OutputRegisterBases::ControlAllRegisters as u16,
            true,
        ))
    }

    pub fn close_all_outputs(&self) -> RequestAdu {
        self.create_request_adu(Request::WriteSingleCoil(
            OutputRegisterBases::ControlAllRegisters as u16,
            false,
        ))
    }

    pub fn write_output_channels<'a>(
        &self,
        actions: [Action; 8],
        coils: &'a mut [u8],
    ) -> RequestAdu<'a> {
        let values = actions.map(|x| x == Action::On);
        let coils = Coils::from_bools(&values, coils).expect("There's no reason this should fail");
        self.create_request_adu(Request::WriteMultipleCoils(
            OutputRegisterBases::OutputChannel as u16,
            coils,
        ))
    }

    pub fn flash_output_on(&self, channel: Channel, interval: u16) -> RequestAdu {
        self.create_request_adu(Request::WriteSingleRegister(
            channel as u16 + OutputRegisterBases::OutputChannelFlashClose as u16,
            interval,
        ))
    }

    pub fn flash_output_off(&self, channel: Channel, interval: u16) -> RequestAdu {
        self.create_request_adu(Request::WriteSingleRegister(
            channel as u16 + OutputRegisterBases::OutputChannelFlashOpen as u16,
            interval,
        ))
    }

    pub fn read_input_channel_status(&self, channel: Channel) -> RequestAdu {
        self.create_request_adu(Request::ReadDiscreteInputs(
            channel as u16 + InputRegisterBases::InputChannels as u16,
            1,
        ))
    }

    pub fn read_input_channels(&self) -> RequestAdu {
        self.create_request_adu(Request::ReadDiscreteInputs(
            InputRegisterBases::InputChannels as u16,
            8,
        ))
    }

    pub fn set_uart_parameters(&self, parity: Parity, baud: Baudrates) -> RequestAdu {
        let value = ((parity as u16) << 8) | (baud as u16);
        self.create_request_adu(Request::WriteSingleRegister(
            HoldingRegisterBases::UartParameters as u16,
            value,
        ))
    }

    pub fn set_output_control_mode(&self, channel: Channel, mode: ControlMode) -> RequestAdu {
        self.create_request_adu(Request::WriteSingleRegister(
            HoldingRegisterBases::ControlMode as u16 + channel as u16,
            mode as u16,
        ))
    }

    pub fn set_device_address(&self, address: u8) -> RequestAdu {
        self.create_request_adu(Request::WriteSingleRegister(
            HoldingRegisterBases::DeviceAddress as u16,
            address.into(),
        ))
    }

    pub fn read_software_version(&self) -> RequestAdu {
        self.create_request_adu(Request::ReadHoldingRegisters(
            HoldingRegisterBases::SoftwareVersion as u16,
            1,
        ))
    }
}

pub enum ControllerError<E> {
    ModbusError(modbus_core::Error),
    TransportError(E),
}

pub struct RelayController<T>
where
    T: Transport,
{
    transport: T,
    relay_module: RelayModule,
    tx_buffer: [u8; 256],
    rx_buffer: [u8; 256],
}

impl<T> RelayController<T>
where
    T: Transport,
{
    pub fn new(unit_id: u8, transport: T) -> Self {
        let relay_module = RelayModule::new(unit_id); // Slave ID = 1
        Self {
            transport,
            relay_module,
            tx_buffer: [0u8; 256],
            rx_buffer: [0u8; 256],
        }
    }

    pub async fn close_io_channel(
        &mut self,
        channel: Channel,
    ) -> Result<(), ControllerError<T::Error>> {
        let request = self.relay_module.write_output_channel(channel, Action::On);
        self.transport
            .execute_transaction(request, &mut self.tx_buffer, &mut self.rx_buffer)
            .await
            .map_err(ControllerError::TransportError)?;
        Ok(())
    }

    pub async fn open_io_channel(
        &mut self,
        channel: Channel,
    ) -> Result<(), ControllerError<T::Error>> {
        let request = self.relay_module.write_output_channel(channel, Action::Off);
        self.transport
            .execute_transaction(request, &mut self.tx_buffer, &mut self.rx_buffer)
            .await
            .map_err(ControllerError::TransportError)?;
        Ok(())
    }
}
