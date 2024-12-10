import serial
import serial.tools.list_ports
from pydantic import BaseModel
from typing import Any, Optional, List
import re
import subprocess

#DEFAULT_CAN_RATE = 1000000
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


def get_can_devices() -> CanConfig:
    pcan = check_for_pcan()
    if pcan:
        return pcan
    serial_can = get_usb_serial_can()
    if len(serial_can):
        return serial_can[0]
    else:
        raise Exception("No Can Devices Available")
