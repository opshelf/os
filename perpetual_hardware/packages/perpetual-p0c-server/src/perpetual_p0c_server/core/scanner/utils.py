import serial
import serial.tools.list_ports
from pydantic import BaseModel
from typing import Optional, Union, List
import serial


class UsbInfo(BaseModel):
    device: str
    description: Optional[str] = None
    hwid: Optional[Union[str, int]] = None
    interface: Optional[str] = None
    location: Optional[str] = None
    manufacturer: Optional[str] = None
    name: Optional[str] = None
    pid: Optional[int] = None
    product: Optional[str] = None
    serial_number: Optional[str] = None
    # usb_description: Optional[Union[str, int]] = None
    # usb_info: Optional[str] = None
    vid: Optional[int] = None


def get_usb_serial() -> List[UsbInfo]:
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
            # "usb_description": port.usb_description,
            # "usb_info":        port.usb_info,
            "vid": port.vid,
        }
        ports.append(UsbInfo(**data))
    return ports


def get_waveshare_port() -> Optional[str]:
    for port in get_usb_serial():
        if port.description and "USBKey" in port.description:
            return port.device.replace("/dev/cu", "/dev/tty")
    return None

def get_waveshare_ports() -> List[str]:
    scanners = []
    for port in get_usb_serial():
        if port.description and "USBKey" in port.description:
            scanners.append(port.device.replace("/dev/cu", "/dev/tty"))
    return scanners

