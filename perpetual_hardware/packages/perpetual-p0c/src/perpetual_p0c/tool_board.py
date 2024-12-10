from can import BusABC, Message
from typing import List
from pydantic import BaseModel
from .common import CanMessageIds
from enum import Enum


class CanCommandIds(int, Enum):
    GetState = 0x01
    SetPullers = 0x02
    MaskPullers = 0x03
    SetCarriage = 0x04
    MaskCarriage = 0x05
    ResetAll = 0x06


class SlotStatus(BaseModel):
    slot_1: bool
    slot_2: bool
    slot_3: bool
    slot_4: bool
    slot_5: bool
    slot_6: bool
    limit_1: bool
    limit_2: bool

    @staticmethod
    def from_buffer(value: int) -> "SlotStatus":
        return SlotStatus(
            slot_1=bool(value & 0x01),
            slot_2=bool(value & 0x02),
            slot_3=bool(value & 0x04),
            slot_4=bool(value & 0x08),
            slot_5=bool(value & 0x10),
            slot_6=bool(value & 0x20),
            limit_1=bool(value & 0x40),
            limit_2=bool(value & 0x80),
        )


class InputStatus(BaseModel):
    forward_actuator: bool
    backward_actuator: bool
    left_beam: bool
    right_beam: bool
    left_presence: bool
    right_presence: bool

    @staticmethod
    def from_buffer(value: int) -> "InputStatus":
        return InputStatus(
            right_presence=bool(value & 0x01),
            left_presence=bool(value & 0x02),
            right_beam=bool(value & 0x04),
            left_beam=bool(value & 0x08),
            backward_actuator=bool(value & 0x10),
            forward_actuator=bool(value & 0x20),
        )


class IoBoard(BaseModel):
    r1: bool
    r2: bool
    r3: bool
    r4: bool
    r5: bool
    r6: bool

    @staticmethod
    def from_buffer(value: int) -> "IoBoard":
        return IoBoard(
            r1=bool(value & 0x01),
            r2=bool(value & 0x02),
            r3=bool(value & 0x04),
            r4=bool(value & 0x08),
            r5=bool(value & 0x10),
            r6=bool(value & 0x20),
        )

    def to_buffer(self) -> List[int]:
        byte1 = 0
        byte1 |= int(self.r1) << 0
        byte1 |= int(self.r2) << 1
        byte1 |= int(self.r3) << 2
        byte1 |= int(self.r4) << 3
        byte1 |= int(self.r5) << 4
        byte1 |= int(self.r6) << 5
        return [byte1]


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


class BoardState(BaseModel):
    carriage: IoBoard
    puller: IoBoard
    slots: SlotStatus
    inputs: InputStatus

    @staticmethod
    def from_buffer(buffer: List[int] | bytearray) -> "BoardState":
        if len(buffer) < 5:
            raise Exception("Invalid Response")
        return BoardState(
            carriage=IoBoard.from_buffer(buffer[4]),
            puller=IoBoard.from_buffer(buffer[3]),
            slots=SlotStatus.from_buffer(buffer[2]),
            inputs=InputStatus.from_buffer(buffer[1]),
        )


class ToolBoard:
    def __init__(self, bus: BusABC, can_id: int = CanMessageIds.TOOL_NODE) -> None:
        self._bus = bus
        self._can_id = can_id
        self._state = BoardState(
            carriage=IoBoard.from_buffer(0),
            puller=IoBoard.from_buffer(0),
            slots=SlotStatus.from_buffer(0),
            inputs=InputStatus.from_buffer(0),
        )

    @property
    def state(self) -> BoardState:
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

    def get_state(self) -> BoardState:
        response = self._run_command(self.default_message([CanCommandIds.GetState.value]))
        state = BoardState.from_buffer(response.data)
        self._state = state
        return self._state

    def set_pullers(self, io: IoBoard) -> BoardState:
        buffer = [CanCommandIds.SetPullers.value]
        buffer.extend(io.to_buffer())
        response = self._run_command(self.default_message(buffer))
        state = BoardState.from_buffer(response.data)
        self._state = state
        return self._state

    def mask_pullers(self, io: IoBoard) -> BoardState:
        buffer = [CanCommandIds.MaskPullers.value]
        buffer.extend(io.to_buffer())
        response = self._run_command(self.default_message(buffer))
        state = BoardState.from_buffer(response.data)
        self._state = state
        return self._state

    def set_carriage(self, io: IoBoard) -> BoardState:
        buffer = [CanCommandIds.SetCarriage.value]
        buffer.extend(io.to_buffer())
        response = self._run_command(self.default_message(buffer))
        state = BoardState.from_buffer(response.data)
        self._state = state
        return self._state

    def mask_carriage(self, io: IoBoard) -> BoardState:
        buffer = [CanCommandIds.MaskCarriage.value]
        buffer.extend(io.to_buffer())
        response = self._run_command(self.default_message(buffer))
        state = BoardState.from_buffer(response.data)
        self._state = state
        return self._state

    def reset_all(self) -> BoardState:
        buffer = [CanCommandIds.ResetAll.value]
        response = self._run_command(self.default_message(buffer))
        state = BoardState.from_buffer(response.data)
        self._state = state
        return self._state
