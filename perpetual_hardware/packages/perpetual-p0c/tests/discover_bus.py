from can import Bus, Message
from typing import Optional, Tuple
from perpetual_p0c.utils import get_can_devices
from perpetual_p0c.common import CanMessageIds

def discover_bus(address_range: Optional[Tuple[int, int]] = None) -> None:
    usb_can = get_can_devices()
    with Bus(interface=usb_can.CAN_INTERFACE, channel=usb_can.CAN_CHANNEL, bitrate=usb_can.CAN_BITRATE, fd=False) as bus:
        if address_range:
            found = []
            for i in range(address_range[0], address_range[1]):
                msg = Message(
                    arbitration_id=i,
                    data=[6, 1, 0, 0, 0, 0, 0],
                    is_extended_id=False,
                )
                bus.send(msg)
                reply = bus.recv(0.05)
                if reply:
                    print(i, reply)
                    found.append(i)
        else:
            found = []
        for id in CanMessageIds:
            print(id.name, id.value)
            msg = Message(
                arbitration_id=id.value,
                data=[6, 1, 0, 0, 0, 0, 0],
                is_extended_id=False,
            )
            bus.send(msg)
            reply = bus.recv(0.1)
            if reply:
                print(f"{id.name} {id.value} {reply}")


if __name__ == "__main__":
    discover_bus(None)
    # discover_bus((100, 137))
    # discover_bus((114, 115))
    # discover_bus((132, 140))
    # discover_bus(None)
