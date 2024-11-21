from can import BusABC, Message
from typing import List, Union
from pydantic import BaseModel
from enum import Enum


class GenericRelayBoardMessage(BaseModel):
    r1: bool = False
    r2: bool = False
    r3: bool = False
    r4: bool = False
    r5: bool = False
    r6: bool = False
    r7: bool = False
    r8: bool = False
    r9: bool = False
    r10: bool = False
    r11: bool = False
    r12: bool = False
    r13: bool = False
    r14: bool = False

    def to_buffer(self) -> List[int]:
        byte1 = 0
        byte2 = 0
        byte1 |= int(self.r1) << 0
        byte1 |= int(self.r2) << 1
        byte1 |= int(self.r3) << 2
        byte1 |= int(self.r4) << 3
        byte1 |= int(self.r5) << 4
        byte1 |= int(self.r6) << 5
        byte1 |= int(self.r7) << 6
        byte1 |= int(self.r8) << 7
        byte2 |= int(self.r9) << 0
        byte2 |= int(self.r10) << 1
        byte2 |= int(self.r11) << 2
        byte2 |= int(self.r12) << 3
        byte2 |= int(self.r13) << 4
        byte2 |= int(self.r14) << 5
        return [byte1, byte2]


class RelayBoardMessage(BaseModel):
    actuator_forward: bool = False
    actuator_reverse: bool = False
    slot_1: bool = False
    slot_2: bool = False
    slot_3: bool = False
    slot_4: bool = False
    slot_5: bool = False
    slot_6: bool = False
    puller_1: bool = False
    puller_2: bool = False
    puller_3: bool = False
    puller_4: bool = False
    puller_5: bool = False
    puller_6: bool = False

    def to_buffer(self) -> List[int]:
        byte1 = 0
        byte2 = 0
        byte1 |= int(self.actuator_forward) << 0
        byte1 |= int(self.actuator_reverse) << 1
        byte1 |= int(self.slot_1) << 2
        byte1 |= int(self.slot_2) << 3
        byte1 |= int(self.slot_3) << 4
        byte1 |= int(self.slot_4) << 5
        byte1 |= int(self.slot_5) << 6
        byte1 |= int(self.slot_6) << 7
        byte2 |= int(self.puller_1) << 0
        byte2 |= int(self.puller_2) << 1
        byte2 |= int(self.puller_3) << 2
        byte2 |= int(self.puller_4) << 3
        byte2 |= int(self.puller_5) << 4
        byte2 |= int(self.puller_6) << 5
        return [byte1, byte2]

class Commads(int, Enum):
    MASK_BITS = 0x01
    SET_BITS = 0x02
    READ_BITS = 0x03

class ToolRelayController:

    def __init__(self, bus: BusABC, can_id: int = 6) -> None:
        self._bus = bus
        self._can_id = can_id

    def default_message(self, data: List[int]) -> Message:
        return Message(
            arbitration_id=self._can_id,
            data=data,
            is_extended_id=False,
        )

    def _send_message(self, message: Message) -> None:
        self._bus.send(message)

    def _receive_message(self) -> Message:
        msg = self._bus.recv(timeout=3.0)
        if msg:
            return msg
        raise Exception(f"No Response From Relay Module")

    def _run_command(self, message: Message) -> Message:
        self._send_message(message)
        return self._receive_message()

    def set_relays(self, msg: Union[RelayBoardMessage, GenericRelayBoardMessage]) -> Message:
        return self._run_command(self.default_message(msg.to_buffer()))

    def mask_bits(self, msg: Union[RelayBoardMessage, GenericRelayBoardMessage]) -> Message:
        data = [Commads.MASK_BITS.value]
        data.extend(msg.to_buffer())
        return self._run_command(self.default_message(data))

    def set_bits(self, msg: Union[RelayBoardMessage, GenericRelayBoardMessage]) -> Message:
        data = [Commads.SET_BITS.value]
        data.extend(msg.to_buffer())
        return self._run_command(self.default_message(data))

    def read_bits(self) -> Message:
        data = [Commads.READ_BITS.value]
        return self._run_command(self.default_message(data))
