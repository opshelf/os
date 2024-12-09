import serial
import struct
from enum import Enum
from typing import Optional, Tuple, Any
from can import Message, BusABC, CanProtocol
import time


class CANUSB_SPEED(Enum):
    SPEED_1000000 = 0x01
    SPEED_800000 = 0x02
    SPEED_500000 = 0x03
    SPEED_400000 = 0x04
    SPEED_250000 = 0x05
    SPEED_200000 = 0x06
    SPEED_125000 = 0x07
    SPEED_100000 = 0x08
    SPEED_50000 = 0x09
    SPEED_20000 = 0x0A
    SPEED_10000 = 0x0B
    SPEED_5000 = 0x0C


class CANUSB_MODE(Enum):
    NORMAL = 0x00
    LOOPBACK = 0x01
    SILENT = 0x02
    LOOPBACK_SILENT = 0x03


class CANUSB_FRAME(Enum):
    STANDARD = 0x01
    EXTENDED = 0x02


class CANUSB_PAYLOAD_MODE(Enum):
    INJECT_PAYLOAD_MODE_RANDOM = 0
    INJECT_PAYLOAD_MODE_INCREMENTAL = 1
    INJECT_PAYLOAD_MODE_FIXED = 2


class Waveshare(BusABC):
    """A class to interact with a USB CAN adapter."""

    CANUSB_TTY_BAUD_RATE_DEFAULT = 2000000
    # CANUSB_TTY_BAUD_RATE_DEFAULT = 1000000
    DATA_START_INDEX = 6
    FRAME_ID_SLICE = slice(2, 6)
    PGN_SLICE = slice(3, 5)

    def __init__(
        self,
        channel: str,
        baudrate: int = CANUSB_TTY_BAUD_RATE_DEFAULT,
        timeout: float = 1.0,
        frame_type: CANUSB_FRAME = CANUSB_FRAME.STANDARD,
        bitrate: CANUSB_SPEED = CANUSB_SPEED.SPEED_1000000,
        # bitrate: CANUSB_SPEED = CANUSB_SPEED.SPEED_500000,
        **kwargs: Any,
    ) -> None:
        self._port = channel
        self._speed = bitrate
        self._frame_type = frame_type
        self._baudrate = baudrate
        self.filter_id = bytearray([0x00, 0x00, 0x00, 0x00])
        self.mask_id = bytearray([0x00, 0x00, 0x00, 0x00])
        self._can_protocol = CanProtocol.CAN_20
        self._timeout = timeout
        self._receive_attempts = 0
        self._device = serial.Serial(
            port=self._port,
            baudrate=self._baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_TWO,
            timeout=self._timeout,  # Add a timeout to prevent blocking indefinitely
        )
        self._device.flush()
        self.configure_device()
        time.sleep(1)
        super().__init__(channel=channel, **kwargs)

    def shutdown(self) -> None:
        """
        Close the serial interface.
        """
        super().shutdown()
        self._device.close()

    @property
    def port(self) -> str:
        return self._port

    @port.setter
    def port(self, name: str) -> None:
        self._port = name

    @property
    def speed(self) -> CANUSB_SPEED:
        return self._speed

    @speed.setter
    def speed(self, speed: CANUSB_SPEED) -> None:
        self._speed = speed

    @property
    def device(self) -> serial.Serial:
        return self._device

    @property
    def frame(self) -> CANUSB_FRAME:
        return self._frame

    @frame.setter
    def frame(self, frame: CANUSB_FRAME) -> None:
        self._frame = frame

    def generate_checksum(self, data: bytearray) -> int:
        """Generates a checksum from the given data bytearray."""
        checksum = sum(data)
        return checksum & 0xFF

    def _write(self, data: bytearray) -> Any:
        return self._device.write(data)

    def format_frame(self, id: int, data: list[int]) -> bytearray:
        frame_type = 0xC0
        frame_type |= len(data) & 0xFF
        frame = [0xAA, frame_type, id & 0xFF, (id >> 8) & 0xFF]
        frame.extend(data)
        frame.append(0x55)
        return bytearray(frame)

    def frame_send(self, message: Message) -> Any:
        if isinstance(message.data, list):
            return self._write(self.format_frame(message.arbitration_id, message.data))
        if isinstance(message.data, bytearray):
            return self._write(self.format_frame(message.arbitration_id, list(message.data)))
        if isinstance(message.data, bytes):
            return self._write(
                self.format_frame(message.arbitration_id, list(bytearray(message.data)))
            )
        raise Exception("Invalid Data Format")

    def send(self, msg: Message, timeout: Optional[float] = None) -> None:
        if timeout:
            self._device.timeout = timeout
        self.frame_send(msg)
        # time.sleep(.1)

    def _read_header(self) -> bool:
        header = self._device.read(1)
        # print(f"CAN header: {header} {self._receive_attempts}")
        if len(header) == 1:
            # print(f"Read Header: {header}")
            if header[0] == 0xAA:
                return True
            else:
                return False
        else:
            return False

    def _read_packet(self) -> Optional[Message]:
        if self._read_header():
            frame_type = struct.unpack(">B", self._device.read(1))[0]
            frame_length = frame_type & 0b1111
            # print(f"Frame Type: {frame_type}")
            frame_id = struct.unpack("<H", self._device.read(2))[0]
            # print(f"Frame ID: {frame_id}")
            frame_data = bytearray(self._device.read(frame_length))
            # print(f"Frame Data: {frame_data}")
            frame_end = struct.unpack(">B", self._device.read(1))[0]
            # print(f"Frame End: {frame_end}")
            if frame_end != 0x55:
                raise Exception("Invalid Frame")
            return Message(
                timestamp=time.time(),
                arbitration_id=frame_id,
                is_extended_id=False,
                dlc=frame_length,
                data=list(frame_data),
            )
        else:
            return None

    def frame_receive(self) -> Optional[Message]:
        return self._read_packet()

    def _recv_internal(self, timeout: Optional[float]) -> Tuple[Optional[Message], bool]:
        packet = self._read_packet()
        if packet:
            self._receive_attempts = 0
            return (packet, False)
        else:
            self._receive_attempts += 1
            return (None, False)

    def configure_device(
        self,
        speed: Optional[CANUSB_SPEED] = CANUSB_SPEED.SPEED_1000000,
        mode: Optional[CANUSB_MODE] = CANUSB_MODE.NORMAL,
        frame: Optional[CANUSB_FRAME] = CANUSB_FRAME.STANDARD,
    ) -> None:
        if speed:
            self._speed = speed
        if mode:
            self._mode = mode
        if frame:
            self._frame = frame
        """
        Configures the CAN to serial adapter settings.

        This function sets up the communication protocol for the CAN to serial adapter. The communication can be in either a fixed 20-byte communication protocol, or a variable length communication protocol based on the settings provided.

        Parameters:
        speed (int, optional): The speed of the CAN bus in bps. It is converted to an enum value based on predefined speed settings. Default is 0 which corresponds to a pre-configured speed setting.
        mode (CANUSB_MODE, optional): The mode in which the CAN to serial adapter operates. The modes can be NORMAL, LOOPBACK, SILENT, or LOOPBACK_SILENT. Default is NORMAL.
        frame (CANUSB_FRAME, optional): The frame format, which can be STANDARD or EXTENDED. Default is STANDARD.

        Variable Length Communication Protocol:
        In this protocol, the packet structure is as follows:
        - Packet Header: 0xaa
        - Type: 0xc0, with various bits indicating frame type, frame format and data length.
        - Frame ID: 2 bytes for standard frame and 4 bytes for extended frame.
        - Frame Data: 0 to 8 data bytes.
        - End Code: 0x55

        Fixed 20-byte Communication Protocol:
        In this protocol, the packet structure is as follows:
        - Packet Header: 0xaa, 0x55
        - Type: 0x01 for Data
        - Frame Type: 0x01 for Standard frame, 0x02 for Extended frame.
        - Frame Format: 0x01 for Data frame, 0x02 for Remote frame.
        - Frame ID Data: 4 bytes, high bytes at the front, low bytes at the back.
        - Frame Data Length: The data length of the CAN bus that is sent or accepted.
        - Frame Data: Up to 8 data bytes.
        - Reserve: 0x00
        - Check Code: Checksum from frame type to error code, accumulating and taking the low 8 bits.
        """  # noqa: E501
        cmd_frame = bytearray()
        cmd_frame.append(0xAA)
        cmd_frame.append(0x55)
        cmd_frame.append(0x12)
        cmd_frame.append(self._speed.value)
        cmd_frame.append(self._frame.value)
        # Fill with zeros for Filter ID and Mask ID (not handled)
        cmd_frame.extend([0] * 8)
        # cmd_frame.extend(self._filter_id)
        # cmd_frame.extend(self._mask_id)
        cmd_frame.append(self._mode.value)
        cmd_frame.extend([0x01, 0, 0, 0, 0])
        cmd_frame.append(self.generate_checksum(cmd_frame[2:19]))
        self._write(cmd_frame)
