import serial
import serial.tools.list_ports
import asyncio
from pydantic import BaseModel
from typing import Any, Optional, List
import re
import subprocess
from can import BusABC, Bus
from perpetual_can import CANBusHandler
import socket
import errno
from typing import Tuple

# DEFAULT_CAN_RATE = 1000000
DEFAULT_CAN_RATE = 500000


class SerialDeviceInfo(BaseModel):
    device: str
    name: str
    description: str
    hwid: str
    vid: int | Any
    pid: int | Any
    serial_number: Optional[str]
    location: Optional[str]
    manufacturer: Optional[str]
    product: Optional[str]
    interface: Optional[str]


class UsbDeviceInfo(BaseModel):
    id: str
    tag: str
    device: str


class CanConfig(BaseModel):
    CAN_INTERFACE: str
    CAN_CHANNEL: str
    CAN_BITRATE: int


def check_for_pcan() -> Optional[CanConfig]:
    device_re = re.compile(
        b"Bus\\s+(?P<bus>\\d+)\\s+Device\\s+(?P<device>\\d+).+ID\\s(?P<id>\\w+:\\w+)\\s(?P<tag>.+)$",
        re.I,
    )
    df = subprocess.check_output("lsusb")
    devices = []
    for i in df.split(b"\n"):
        if i:
            info = device_re.match(i)
            if info:
                dinfo = info.groupdict()
                dinfo["device"] = "/dev/bus/usb/%s/%s" % (
                    dinfo.pop("bus").decode(),
                    dinfo.pop("device").decode(),
                )
                devices.append(dinfo)
    for device in devices:
        if "pcan" in device["tag"].decode().lower():
            return CanConfig(
                CAN_INTERFACE="pcan",
                CAN_CHANNEL="PCAN_USBBUS1",
                CAN_BITRATE=DEFAULT_CAN_RATE,
            )
    return None


def get_usb_serial_can() -> List[CanConfig]:
    out = []
    for port in serial.tools.list_ports.comports():
        if "serial" in port.description.lower():
            out.append(
                CanConfig(
                    CAN_INTERFACE="waveshare",
                    CAN_CHANNEL=port.device,
                    CAN_BITRATE=DEFAULT_CAN_RATE,
                )
            )
        if "can" in port.description.lower():
            out.append(
                CanConfig(
                    CAN_INTERFACE="slcan",
                    CAN_CHANNEL=port.device,
                    CAN_BITRATE=DEFAULT_CAN_RATE,
                )
            )
    return out


def is_can_available(interface: str) -> Tuple[bool, str]:
    try:
        sock = socket.socket(socket.PF_CAN, socket.SOCK_RAW, socket.CAN_RAW)  # type: ignore
    except AttributeError:
        return False, "CAN socket support not available in the kernel"
    try:
        sock.bind((interface,))
        available = True
        message = f"CAN interface {interface} is available and accessible"
    except socket.error as e:
        available = False
        if e.errno == errno.ENODEV:
            message = f"CAN interface {interface} does not exist"
        elif e.errno == errno.EACCES:
            message = f"Permission denied to access CAN interface {interface}"
        elif e.errno == errno.ENETDOWN:
            message = f"CAN interface {interface} is down"
        else:
            message = f"Error checking CAN interface {interface}: {str(e)}"
    finally:
        sock.close()
    return available, message


def check_socket_can() -> Optional[CanConfig]:
    interfaces = [
        "can0",
        "can1",
    ]
    for interface in interfaces:
        available, _ = is_can_available(interface)
        if available:
            return CanConfig(CAN_INTERFACE="socketcan", CAN_CHANNEL=interface, CAN_BITRATE=500000)
    return None


def get_can_devices() -> CanConfig:
    pcan = check_for_pcan()
    if pcan:
        return pcan
    socket_can = check_socket_can()
    if socket_can:
        return socket_can
    serial_can = get_usb_serial_can()
    if len(serial_can):
        return serial_can[0]
    else:
        raise Exception("No Can Devices Available")


def get_bus() -> BusABC:
    can_device = get_can_devices()
    return Bus(
        interface=can_device.CAN_INTERFACE,
        channel=can_device.CAN_CHANNEL,
        bitrate=can_device.CAN_BITRATE,
        fd=False,
    )


async def get_can_bus() -> CANBusHandler:
    can_device = get_bus()
    return CANBusHandler(can_device, asyncio.get_event_loop())


def get_usb_serial() -> List[SerialDeviceInfo]:
    ports = []
    for port in serial.tools.list_ports.comports():
        data = {
            "description": port.description,
            "device": port.device,
            "hwid": port.hwid,
            "interface": port.interface,
            "location": port.location,
            "manufacturer": port.manufacturer,
            "name": port.name,
            "pid": port.pid,
            "product": port.product,
            "serial_number": port.serial_number,
            "vid": port.vid,
        }
        ports.append(SerialDeviceInfo(**data))
    return ports


def get_trinamic_usb() -> SerialDeviceInfo:
    for port in get_usb_serial():
        if port.description and "Trinamic" in port.description:
            return port
    raise Exception("No Device Found")
