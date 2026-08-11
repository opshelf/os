import logging
import threading

try:
    import smbus2 as smbus
except ModuleNotFoundError:
    smbus = None

try:
    import RPi.GPIO as GPIO
except (ModuleNotFoundError, RuntimeError):
    GPIO = None

# PCA9535 Register Addresses
REG_INPUT_PORT0     = 0x00
REG_INPUT_PORT1     = 0x01
REG_OUTPUT_PORT0    = 0x02
REG_OUTPUT_PORT1    = 0x03
REG_INVERSION_PORT0 = 0x04
REG_INVERSION_PORT1 = 0x05
REG_CONFIG_PORT0    = 0x06
REG_CONFIG_PORT1    = 0x07

# PCA9535 Auto-Increment Register Pairs (Command Bytes)
REG_INPUT_PAIR    = 0x00  # Maps to Input Port 0, Input Port 1
REG_OUTPUT_PAIR   = 0x02  # Maps to Output Port 0, Output Port 1
REG_INVERSION_PAIR = 0x04 # Maps to Polarity Inversion Port 0, Polarity Inversion Port 1
REG_CONFIG_PAIR   = 0x06  # Maps to Configuration Port 0, Configuration Port 1

def _validate_byte(name, value):
    if not (0 <= value <= 0xFF):
        raise ValueError(f"{name} must be an integer between 0 and 255")

class PCA9535:
    def __init__(self, bus_num=1, address=0x20, interrupt_pin=None,
                 port_direction=None, port_inversion=None, port_output_value=None):
        """
        Initializes the thread-safe PCA9535 device using block-based I2C transactions.

        :param bus_num: I2C bus number.
        :param address: I2C device address.
        :param interrupt_pin: RPi GPIO pin number used for INT notification.
        :param port_direction: List of two 8-bit integers [Port0, Port1] (0=Output, 1=Input).
        :param port_inversion: List of two 8-bit integers [Port0, Port1] (0=Normal, 1=Inverted).
        :param port_output_value: List of two 8-bit integers [Port0, Port1] initial output states.
        """
        self.logger = logging.getLogger(__name__)
        if smbus is None:
            raise RuntimeError("smbus2 is not available; cannot open an I2C bus")
        self.bus = smbus.SMBus(bus_num)
        self.address = address
        self.interrupt_pin = interrupt_pin
        self._interrupt_registered = False
        self._bus_closed = False

        # Lock to ensure mutual exclusion across threads and ISR contexts during I2C operations
        self._lock = threading.Lock()

        # Instance variable to store the latest raw input register values
        self.latest_input = [0x00, 0x00]

        try:
            # Writing output/inversion values without also setting direction leaves the
            # pins in whatever direction they already were (inputs, on power-up), so the
            # writes have no electrical effect until port_direction is set separately.
            if (port_output_value is not None or port_inversion is not None) and port_direction is None:
                self.logger.warning(
                    "port_output_value/port_inversion provided without port_direction; "
                    "pins will not actually change direction until config_write() is called."
                )

            # Apply initial hardware states if provided
            if port_output_value is not None:
                self.port_output_write(port_output_value[0], port_output_value[1])
            if port_inversion is not None:
                self.port_invert_write(port_inversion[0], port_inversion[1])
            if port_direction is not None:
                self.config_write(port_direction[0], port_direction[1])

            # Configure interrupt pin if provided
            if self.interrupt_pin is not None:
                if GPIO is None:
                    raise RuntimeError("RPi.GPIO is not available; cannot register an interrupt pin")
                current_mode = GPIO.getmode()
                if current_mode is None:
                    GPIO.setmode(GPIO.BCM)
                elif current_mode != GPIO.BCM:
                    raise RuntimeError(
                        "RPi.GPIO mode already set to something other than BCM; "
                        "PCA9535 interrupt handling requires BCM pin numbering."
                    )
                GPIO.setup(self.interrupt_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
                # RPi.GPIO callbacks run on a separate background thread.
                # The PCA9535's INT line is active-low and open-drain; it deasserts
                # (rises) as a side effect of reading the input port, including the
                # read this same handler performs. Watching only the falling edge
                # avoids re-triggering the callback on that self-inflicted rising edge.
                GPIO.add_event_detect(self.interrupt_pin, GPIO.FALLING, callback=self.service_interrupt)
                self._interrupt_registered = True
        except Exception:
            try:
                self.close()
            except Exception:
                pass
            raise

    def service_interrupt(self, pin):
        """
        ISR function to handle falling-edge interrupt events (INT asserted).
        Thread-safe read clears the chip flag and updates internal cache state.
        """
        pin_state = GPIO.input(pin)
        self.logger.debug(f"PCA9535 Interrupt event triggered on GPIO pin {pin}. Pin level: {pin_state}")

        try:
            # Reading the input registers clears the internal interrupt flag of the PCA9535
            p0, p1 = self.port_input_read()
        except Exception:
            # Exceptions raised on RPi.GPIO's callback thread are otherwise
            # swallowed/printed by the library and easy to miss.
            self.logger.exception("PCA9535 failed to read input registers during interrupt handling")
            return

        with self._lock:
            self.latest_input = [p0, p1]

        self.logger.info(f"Interrupt handled. Latest input cache updated to: Port0=0x{p0:02X}, Port1=0x{p1:02X}")

    def get_latest_input(self):
        """Thread-safe accessor for the input values most recently cached by service_interrupt()."""
        with self._lock:
            return tuple(self.latest_input)

    def close(self):
        """Tears down owned GPIO and I2C resources."""
        with self._lock:
            cleanup_error = None

            if self._interrupt_registered:
                self._interrupt_registered = False
                try:
                    GPIO.remove_event_detect(self.interrupt_pin)
                except Exception as exc:
                    cleanup_error = exc

            if self._bus_closed is False:
                self._bus_closed = True
                try:
                    self.bus.close()
                except Exception as exc:
                    if cleanup_error is None:
                        cleanup_error = exc

            if cleanup_error is not None:
                raise cleanup_error

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    # --- PORT FUNCTIONS ---

    def port_input_read(self):
        """Thread-safe block read of data from Input Port 0 and Input Port 1 registers."""
        with self._lock:
            data = self.bus.read_i2c_block_data(self.address, REG_INPUT_PAIR, 2)
        return data[0], data[1]

    def port_output_read(self):
        """Thread-safe block read of current configured Output Port 0 and Output Port 1 data."""
        with self._lock:
            data = self.bus.read_i2c_block_data(self.address, REG_OUTPUT_PAIR, 2)
        return data[0], data[1]

    def port_output_write(self, port0_val, port1_val):
        """Thread-safe block write of values to Output Port 0 and Output Port 1 registers."""
        _validate_byte("port0_val", port0_val)
        _validate_byte("port1_val", port1_val)
        with self._lock:
            self.bus.write_i2c_block_data(self.address, REG_OUTPUT_PAIR, [port0_val, port1_val])

    def config_read(self):
        """Thread-safe block read of Configuration Register 0 and Configuration Register 1 values."""
        with self._lock:
            data = self.bus.read_i2c_block_data(self.address, REG_CONFIG_PAIR, 2)
        return data[0], data[1]

    def config_write(self, port0_config, port1_config):
        """Thread-safe block write setup to Configuration Register 0 and Configuration Register 1 (0=Output, 1=Input)."""
        _validate_byte("port0_config", port0_config)
        _validate_byte("port1_config", port1_config)
        with self._lock:
            self.bus.write_i2c_block_data(self.address, REG_CONFIG_PAIR, [port0_config, port1_config])

    def port_invert_read(self):
        """Thread-safe block read of Polarity Inversion Register 0 and Polarity Inversion Register 1."""
        with self._lock:
            data = self.bus.read_i2c_block_data(self.address, REG_INVERSION_PAIR, 2)
        return data[0], data[1]

    def port_invert_write(self, port0_inv, port1_inv):
        """Thread-safe block write setup to Polarity Inversion Register 0 and Polarity Inversion Register 1."""
        _validate_byte("port0_inv", port0_inv)
        _validate_byte("port1_inv", port1_inv)
        with self._lock:
            self.bus.write_i2c_block_data(self.address, REG_INVERSION_PAIR, [port0_inv, port1_inv])

    # --- PIN FUNCTIONS ---

    def pin_config_write(self, pin_index, direction):
        """
        Configures the direction of a specific pin index from 0 to 15.
        Uses a thread-safe read-modify-write cycle.

        :param pin_index: Integer index (0 to 15).
        :param direction: 0 for Output, 1 for Input.
        """
        if not (0 <= pin_index <= 15):
            raise ValueError("pin_index must be an integer between 0 and 15")
        if direction not in (0, 1):
            raise ValueError("direction must be 0 (Output) or 1 (Input)")

        with self._lock:
            # Perform a block read to get current state atomically
            data = self.bus.read_i2c_block_data(self.address, REG_CONFIG_PAIR, 2)
            p0, p1 = data[0], data[1]

            if pin_index < 8:
                p0 = (p0 & ~(1 << pin_index)) | (direction << pin_index)
            else:
                p1 = (p1 & ~(1 << (pin_index - 8))) | (direction << (pin_index - 8))

            # Commit the update atomically using block write
            self.bus.write_i2c_block_data(self.address, REG_CONFIG_PAIR, [p0, p1])

    def pin_invert_write(self, pin_index, invert):
        """
        Configures the polarity inversion of a specific pin index from 0 to 15.
        Uses a thread-safe read-modify-write cycle.

        :param pin_index: Integer index (0 to 15).
        :param invert: 0 for Normal polarity, 1 for Inverted polarity.
        """
        if not (0 <= pin_index <= 15):
            raise ValueError("pin_index must be an integer between 0 and 15")
        if invert not in (0, 1):
            raise ValueError("invert must be 0 (Normal) or 1 (Inverted)")

        with self._lock:
            # Perform a block read to get current inversion state atomically
            data = self.bus.read_i2c_block_data(self.address, REG_INVERSION_PAIR, 2)
            p0, p1 = data[0], data[1]

            if pin_index < 8:
                p0 = (p0 & ~(1 << pin_index)) | (invert << pin_index)
            else:
                p1 = (p1 & ~(1 << (pin_index - 8))) | (invert << (pin_index - 8))

            # Commit the update atomically using block write
            self.bus.write_i2c_block_data(self.address, REG_INVERSION_PAIR, [p0, p1])

    def pin_input_read(self, pin_index):
        """
        Reads the input value of a specific pin index from 0 to 15.
        Pins 0-7 map to Port 0 (bits 0-7). Pins 8-15 map to Port 1 (bits 0-7).

        :param pin_index: Integer index (0 to 15).
        """
        if not (0 <= pin_index <= 15):
            raise ValueError("pin_index must be an integer between 0 and 15")

        p0, p1 = self.port_input_read()

        if pin_index < 8:
            return (p0 >> pin_index) & 1
        else:
            return (p1 >> (pin_index - 8)) & 1

    def pin_output_write(self, pin_index, level):
        """
        Writes a high or low level to a specific output pin index from 0 to 15.
        Uses a thread-safe read-modify-write cycle.

        :param pin_index: Integer index (0 to 15).
        :param level: 0 for Low, 1 for High.
        """
        if not (0 <= pin_index <= 15):
            raise ValueError("pin_index must be an integer between 0 and 15")

        with self._lock:
            # Perform a block read to get current state atomically
            data = self.bus.read_i2c_block_data(self.address, REG_OUTPUT_PAIR, 2)
            p0, p1 = data[0], data[1]

            if pin_index < 8:
                p0 = (p0 & ~(1 << pin_index)) | (level << pin_index)
            else:
                p1 = (p1 & ~(1 << (pin_index - 8))) | (level << (pin_index - 8))

            # Commit the update atomically using block write
            self.bus.write_i2c_block_data(self.address, REG_OUTPUT_PAIR, [p0, p1])

    # --- DEVICE RESET ---

    def reset(self):
        """
        Resets the device registers to default conditions.
        First sets all pins to inputs (0xFF), then clears output and inversion configurations (0x00).
        """
        self.logger.info(f"Executing thread-safe block soft reset sequence on PCA9535 at address 0x{self.address:02X}")
        with self._lock:
            # 1. Configuration Registers: Power-up default sets all pins to Inputs (0xFF)
            self.bus.write_i2c_block_data(self.address, REG_CONFIG_PAIR, [0xFF, 0xFF])
            # 2. Output Registers: Power-up default sets all outputs to High (0xFF) or Low (0x00). Clearing here.
            self.bus.write_i2c_block_data(self.address, REG_OUTPUT_PAIR, [0x00, 0x00])
            # 3. Polarity Inversion Registers: Power-up default clears inversion (0x00)
            self.bus.write_i2c_block_data(self.address, REG_INVERSION_PAIR, [0x00, 0x00])
