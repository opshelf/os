use defmt::Format;

#[derive(Copy, Clone, PartialEq, Eq, Format)]
#[repr(u8)]
#[allow(non_camel_case_types)]
pub enum Registers {
    /* General configuration registers */
    /// Global configuration flags
    GCONF = 0x00,
    /// Global status flags
    GSTAT = 0x01,
    /// UART transmission counter
    IFCNT = 0x02,
    /// UART slave configuration
    NODECONF = 0x03,
    /// Read input / write output pins
    IOIN = 0x04,
    /// Position comparison register
    X_COMPARE = 0x05,
    // /* Velocity dependent driver feature control registers */
    // /// Driver current control
    IHOLD_IRUN = 0x10,
    /// Delay before power down
    TPOWERDOWN = 0x11,
    /// Actual time between microsteps
    TSTEP = 0x12,
    /// Upper velocity for stealthChop voltage PWM mode
    TPWMTHRS = 0x13,
    /// Lower threshold velocity for switching on smart energy coolStep and stallGuard feature
    TCOOLTHRS = 0x14,
    /// Velocity threshold for switching into a different chopper mode and fullstepping
    THIGH = 0x15,

    /* Ramp generator motion control registers */
    /// Driving mode (Velocity, Positioning, Hold)
    RAMPMODE = 0x20,
    /// Actual motor position
    XACTUAL = 0x21,
    /// Actual  motor  velocity  from  ramp  generator
    VACTUAL = 0x22,
    /// Motor start velocity
    VSTART = 0x23,
    /// First acceleration between VSTART and V1
    A1 = 0x24,
    /// First acceleration/deceleration phase target velocity
    V1 = 0x25,
    /// Second acceleration between V1 and VMAX
    AMAX = 0x26,
    /// Target velocity in velocity mode
    VMAX = 0x27,
    /// Deceleration between VMAX and V1
    DMAX = 0x28,
    /// Deceleration between V1 and VSTOP
    /// Attention:  Do  not  set  0  in  positioning  mode, even if V1=0!
    D1 = 0x2A,
    /// Motor stop velocity
    /// Attention: Set VSTOP > VSTART!
    /// Attention:  Do  not  set  0  in  positioning  mode, minimum 10 recommend!
    VSTOP = 0x2B,
    /// Waiting time after ramping down to zero velocity before next movement or direction inversion can start.
    TZEROWAIT = 0x2C,
    /// Target position for ramp mode
    XTARGET = 0x2D,

    /* Ramp generator driver feature control registers */
    /// Velocity threshold for enabling automatic commutation dcStep
    VDCMIN = 0x33,
    /// Switch mode configuration
    SW_MODE = 0x34,
    /// Ramp status and switch event status
    RAMP_STAT = 0x35,
    /// Ramp generator latch position upon programmable switch event
    XLATCH = 0x36,

    /* Encoder registers */
    /// Encoder configuration and use of N channel
    ENCMODE = 0x38,
    /// Actual encoder position
    X_ENC = 0x39,
    /// Accumulation constant
    ENC_CONST = 0x3A,
    /// Encoder status information
    ENC_STATUS = 0x3B,
    /// Encoder position latched on N event
    ENC_LATCH = 0x3C,
    /// Maximum number of steps deviation between encoder counter and XACTUAL for deviation warning
    ENC_DEVIATION = 0x3D,

    /* Motor driver registers */
    /// Microstep table entries. Add 0...7 for the next registers
    MSLUT_0_7 = 0x60,
    /// Look up table segmentation definition
    MSLUTSEL = 0x68,
    /// Absolute current at microstep table entries 0 and 256
    MSLUTSTART = 0x69,
    /// Actual position in the microstep table
    MSCNT = 0x6A,
    /// Actual microstep current
    MSCURACT = 0x6B,
    /// Chopper and driver configuration
    CHOPCONF = 0x6C,
    /// coolStep smart current control register and stallGuard2 configuration
    COOLCONF = 0x6D,
    /// dcStep automatic commutation configuration register
    DCCTRL = 0x6E,
    /// stallGuard2 to_val and driver error flags
    DRV_STATUS = 0x6F,
    /// stealthChop voltage PWM mode chopper configuration
    PWMCONF = 0x70,
    /// Results of stealthChop amplitude regulator.
    PWM_SCALE = 0x71,
    /// Encoder mode configuration for a special mode (enc_commutation), not for normal use
    ENCM_CTRL = 0x72,
    /// Number of input steps skipped due to dcStep. only with SD_MODE = 1
    LOST_STEPS = 0x73,
}

impl Registers {
    pub fn addr(&self) -> u8 {
        *self as u8
    }
}

/*
GCONF         = 0x00, BITFIELD
GSTAT         = 0x01, BITFIELD  READ ONLY
IFCNT         = 0x02, u8        READ ONLY
NODECONF      = 0x03, BITFIELD
IOIN          = 0x04, BTFIELD
X_COMPARE     = 0x05, i32/u32?
IHOLD_IRUN    = 0x10, BITFIELD
TPOWERDOWN    = 0x11, u8
TSTEP         = 0x12,           READ ONLY
TPWMTHRS      = 0x13,
TCOOLTHRS     = 0x14,
THIGH         = 0x15,
RAMPMODE      = 0x20,
XACTUAL       = 0x21,
VACTUAL       = 0x22,
VSTART        = 0x23,
A1            = 0x24,
V1            = 0x25,
AMAX          = 0x26,
VMAX          = 0x27,
DMAX          = 0x28,
D1            = 0x2A,
VSTOP         = 0x2B,
TZEROWAIT     = 0x2C,
XTARGET       = 0x2D,
VDCMIN        = 0x33,
SW_MODE       = 0x34,
RAMP_STAT     = 0x35,
XLATCH        = 0x36,
X_ENC         = 0x39,
ENC_CONST     = 0x3A,
ENC_STATUS    = 0x3B,
ENC_LATCH     = 0x3C,
ENC_DEVIATION = 0x3D,
MSLUT_0_7     = 0x60,
MSLUTSEL      = 0x68,
MSLUTSTART    = 0x69,
MSCNT         = 0x6A,
MSCURACT      = 0x6B,
CHOPCONF      = 0x6C,
COOLCONF      = 0x6D,
DCCTRL        = 0x6E,
DRV_STATUS    = 0x6F,
PWMCONF       = 0x70,
PWM_SCALE     = 0x71,
ENCM_CTRL     = 0x72,
LOST_STEPS    = 0x73,
*/
