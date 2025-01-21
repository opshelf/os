from can import Message
from typing import List, Optional
from pydantic import BaseModel
from perpetual_p0c.common import CanMessageIds
from perpetual_can import CANBusHandler, AsyncFilteredReader
from enum import Enum


class IoBoardNoMessageReceived(Exception):
    """No Message Received From IO Board"""


class CanCommandIds(int, Enum):
    GetState = 0x01
    SetWedges = 0x02
    SetActuators = 0x03
    ResetValves = 0x04
    SetAllValves = 0x05
    GetSensors = 0x06
    ResetSensors = 0x07


class CanResponseIds(int, Enum):
    Invalid = 0x00
    State = 0x01
    Sensors = 0x02


class SensorState(BaseModel):
    output_conveyor: bool = False
    input_conveyor: bool = False
    output_transfer_conveyor: bool = False
    funnel_input: bool = False
    output_box: bool = False

    @staticmethod
    def from_buffer(data: List[int] | bytearray) -> "Optional[SensorState]":
        if len(data) < 2:
            raise Exception("Invalid Frame")
        elif data[2]:
            value = data[1]
            return SensorState(
                output_conveyor=bool(value & 0x01),
                input_conveyor=bool(value & 0x02),
                output_transfer_conveyor=bool(value & 0x04),
                funnel_input=bool(value & 0x08),
                output_box=bool(value & 0x10),
            )
        else:
            return None


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
    def __init__(self, bus: CANBusHandler, can_id: int = CanMessageIds.IO_NODE) -> None:
        self._bus = bus
        self._can_id = int(200)
        self._reader = AsyncFilteredReader([201])
        self._bus.add_listener(self._reader)
        self._state = IoState(wedges=Wedge(), actuators=ActuatorState.Retracted)

    @property
    def state(self) -> IoState:
        return self._state

    def default_message(self, data: List[int]) -> Message:
        while len(data) < 8:
            data.append(0)
        return Message(
            arbitration_id=self._can_id,
            data=data,
            is_extended_id=False,
        )

    async def _send_message(self, message: Message) -> None:
        await self._bus.send_message(message)

    async def _receive_message(self) -> Message:
        msg = await self._reader.get_message(2.0)
        if msg:
            return msg
        raise Exception("No Response From Relay Module")

    async def _run_command(self, message: Message) -> Message:
        #await self._send_message(message)
        #message = await self._receive_message()
        #if message.data[0]:
        #    return message
        #else:
        #    raise Exception("Invalid Command")
        message = await self._bus.send_ack_message(message, self._reader, 1.0)
        if message.is_error_frame:
            raise Exception("Io Board Encoutered Error Frame")
        return message


    async def get_state(self) -> IoState:
        response = await self._run_command(self.default_message([CanCommandIds.GetState.value]))
        state = IoState.from_buffer(response.data)
        self._state = state
        return self._state

    async def set_wedges(self, wedge: Wedge) -> IoState:
        buffer = [CanCommandIds.SetWedges.value]
        buffer.extend(wedge.to_buffer())
        response = await self._run_command(self.default_message(buffer))
        state = IoState.from_buffer(response.data)
        self._state = state
        return self._state

    async def set_actuators(self, actuator_state: ActuatorState) -> IoState:
        buffer = [CanCommandIds.SetActuators.value]
        buffer.extend(actuator_state.to_buffer())
        response = await self._run_command(self.default_message(buffer))
        state = IoState.from_buffer(response.data)
        self._state = state
        return self._state

    async def reset_valves(self) -> IoState:
        buffer = [CanCommandIds.ResetValves.value]
        response = await self._run_command(self.default_message(buffer))
        state = IoState.from_buffer(response.data)
        self._state = state
        return self._state

    async def set_all_valves(self) -> IoState:
        buffer = [CanCommandIds.SetAllValves.value]
        response = await self._run_command(self.default_message(buffer))
        state = IoState.from_buffer(response.data)
        self._state = state
        return self._state

    async def get_sensors(self) -> Optional[SensorState]:
        buffer = [CanCommandIds.GetSensors.value]
        response = await self._run_command(self.default_message(buffer))
        state = SensorState.from_buffer(response.data)
        return state

    async def reset_sensors(self) -> Optional[SensorState]:
        buffer = [CanCommandIds.ResetSensors.value]
        response = await self._run_command(self.default_message(buffer))
        return SensorState.from_buffer(response.data)
