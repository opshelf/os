#![no_std]
#![no_main]

use embassy_executor::Spawner;
use embassy_stm32::time::Hertz;
use embassy_stm32::Config;
use embedded_can::StandardId;
use {defmt_rtt as _, panic_probe as _};

pub mod board;

#[embassy_executor::main]
async fn main(spawner: Spawner) {
    let config = init();
    let can_id = embedded_can::Id::Standard(StandardId::new(6).unwrap());
    let mut hw = board::NucleoG474::new(can_id, config, spawner);
    hw.run().await;
}

pub fn init() -> Config {
    let mut config = Config::default();
    {
        use embassy_stm32::rcc::*;
        config.rcc.hse = Some(Hse {
            freq: Hertz(24_000_000),
            mode: HseMode::Oscillator,
        });
        config.rcc.pll = Some(Pll {
            source: PllSource::HSE,
            prediv: PllPreDiv::DIV8,
            mul: PllMul::MUL80,
            divp: None,
            // Q Running at 42.5 Mhz
            divq: Some(PllQDiv::DIV8),
            // Main system clock at 170 MHz
            divr: Some(PllRDiv::DIV2),
        });
        config.rcc.sys = Sysclk::PLL1_R;
        config.rcc.mux.fdcansel = mux::Fdcansel::PLL1_Q;
    }
    config
}
