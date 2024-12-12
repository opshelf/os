from can import BusABC, Message
from typing import List
from pydantic import BaseModel
from perpetual_p0c.common import CanMessageIds
from enum import Enum


class CanCommandIds(int, Enum):
    GetState = 0x01
    SetWedges = 0x02
    SetActuators = 0x03
    ResetValves = 0x04
    SetAllValves = 0x05


class Wedge(BaseModel):
    wedge_1: bool = False
    wedge_2: bool = False
    wedge_3: bool = False

    def to_buffer(self) -> List[int]:
        byte1 = 0
        byte1 |= int(self.wedge_1) << 0
        byte1 |= int(self.wedge_2) << 1
        byte1 |= int(self.wedge_3) << 2
        return [byte1]

    @staticmethod
    def from_buffer(value: int) -> "Wedge":
        return Wedge(
            wedge_1=bool(value & 0x01),
            wedge_2=bool(value & 0x02),
            wedge_3=bool(value & 0x04),
        )


class ActuatorState(int, Enum):
    Extended = 0x01
    Retracted = 0x00

    def to_buffer(self) -> List[int]:
        return [self.value]


class IoState(BaseModel):
    wedges: Wedge
    actuators: ActuatorState

    @staticmethod
    def from_buffer(buffer: List[int] | bytearray) -> "IoState":
        if len(buffer) < 3:
            raise Exception("Invalid Response")
        return IoState(
            wedges=Wedge.from_buffer(buffer[1]),
            actuators=ActuatorState.Extended if buffer[2] else ActuatorState.Retracted,
        )


class IoController:
    def __init__(self, bus: BusABC, can_id: int = CanMessageIds.PNEUMATIC_NODE) -> None:
        self._bus = bus
        self._can_id = can_id
        self._state = IoState(wedges=Wedge(), actuators=ActuatorState.Retracted)

    @property
    def state(self) -> IoState:
        return self._state

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
        raise Exception("No Response From Relay Module")

    def _run_command(self, message: Message) -> Message:
        self._send_message(message)
        message = self._receive_message()
        if message.data[0]:
            return message
        else:
            raise Exception("Invalid Command")

    def get_state(self) -> IoState:
        response = self._run_command(self.default_message([CanCommandIds.GetState.value]))
        state = IoState.from_buffer(response.data)
        self._state = state
        return self._state

    def set_wedges(self, wedge: Wedge) -> IoState:
        buffer = [CanCommandIds.SetWedges.value]
        buffer.extend(wedge.to_buffer())
        response = self._run_command(self.default_message(buffer))
        state = IoState.from_buffer(response.data)
        self._state = state
        return self._state

    def set_actuators(self, actuator_state: ActuatorState) -> IoState:
        buffer = [CanCommandIds.SetActuators.value]
        buffer.extend(actuator_state.to_buffer())
        response = self._run_command(self.default_message(buffer))
        state = IoState.from_buffer(response.data)
        self._state = state
        return self._state

    def reset_valves(self) -> IoState:
        buffer = [CanCommandIds.ResetValves.value]
        response = self._run_command(self.default_message(buffer))
        state = IoState.from_buffer(response.data)
        self._state = state
        return self._state

    def set_all_valves(self) -> IoState:
        buffer = [CanCommandIds.SetAllValves.value]
        response = self._run_command(self.default_message(buffer))
        state = IoState.from_buffer(response.data)
        self._state = state
        return self._state
