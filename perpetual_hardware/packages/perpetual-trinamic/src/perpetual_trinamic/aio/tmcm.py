from can import AsyncBufferedReader, BusABC, Message, Notifier
from perpetual_trinamic.tmcl import TMCLCommand, TmclMessage, TmclReply, MoveType
import struct
import asyncio


class TMCM:
    def __init__(
        self,
        connection: BusABC,
        notifier: Notifier,
        module_id: int = 1,
        motor_id: int = 0,
    ) -> None:
        self._connection = connection
        self._reader = AsyncBufferedReader()
        self._notifier = notifier
        self._module_id = module_id & 0xFF
        self._motor_id = motor_id & 0xFF
        self._sync_read = True
        self._notifier.add_listener(self._reader)

    def _send_raw_message(self, message: Message) -> None:
        self._connection.send(message)

    async def _recv_raw_message(self) -> Message:
        return await self._reader.get_message()

    async def _send_message(self, message: TmclMessage) -> TmclReply:
        payload = [message.command_number, message.type_number, message.motor_number]
        payload.extend(list(struct.pack(">i", message.value)))
        # self._connection.send(Message(
        self._connection.send(
            Message(
                arbitration_id=int(message.module_address),
                data=payload,
                is_extended_id=False,
            )
        )
        return await self._receive_message()

    async def _receive_message(self) -> TmclReply:
        async with asyncio.timeout(2.0):
            msg = await self._reader.get_message()
            if msg.arbitration_id == self._module_id:
                if len(msg.data) == 7:
                    return TmclReply(
                        module_address=msg.data[0],
                        status=msg.data[1],
                        command_number=msg.data[2],
                        value=struct.unpack(">i", msg.data[3:])[0],
                    )
                else:
                    raise Exception(f"Invalid Data Format: {msg}")
            else:
                raise Exception(f"Invalid Module Response {self._module_id}")

    def _default_message(self, command_number: int, type_number: int, value: int) -> TmclMessage:
        return TmclMessage(
            module_address=self._module_id,
            command_number=command_number,
            type_number=type_number,
            motor_number=self._motor_id,
            value=value,
        )

    async def rotate_right(self, velocity: int) -> TmclReply:
        return await self._send_message(self._default_message(TMCLCommand.ROR, 0, velocity))

    async def rotate_left(self, velocity: int) -> TmclReply:
        return await self._send_message(self._default_message(TMCLCommand.ROL, 0, velocity))

    async def stop(self) -> TmclReply:
        return await self._send_message(self._default_message(TMCLCommand.MST, 0, 0))

    async def move(self, type: MoveType, value: int) -> TmclReply:
        return await self._send_message(self._default_message(TMCLCommand.MVP, type.value, value))

    async def move_absolute(self, value: int) -> TmclReply:
        return await self._send_message(
            self._default_message(TMCLCommand.MVP, MoveType.ABSOLUTE, value)
        )

    async def move_relative(self, value: int) -> TmclReply:
        return await self._send_message(
            self._default_message(TMCLCommand.MVP, MoveType.RELATIVE, value)
        )

    async def set_axis_parameter(self, axis_parameter: int, value: int) -> TmclReply:
        return await self._send_message(
            self._default_message(TMCLCommand.SAP, axis_parameter, value)
        )

    async def set_io(self, type_number: int, value: int, bank_number: int = 0) -> TmclReply:
        return await self._send_message(
            TmclMessage(
                module_address=self._module_id,
                command_number=TMCLCommand.SIO,
                type_number=type_number,
                motor_number=bank_number,
                value=value,
            )
        )

    async def set_global_parameter(self, parameter: int, value: int) -> TmclReply:
        return await self._send_message(self._default_message(TMCLCommand.SGP, parameter, value))

    async def get_axis_parameter(self, axis_parameter: int) -> TmclReply:
        return await self._send_message(self._default_message(TMCLCommand.GAP, axis_parameter, 0))

    async def get_digital_input(self, port_number: int, bank_number: int) -> TmclReply:
        return await self._send_message(
            TmclMessage(
                module_address=self._module_id,
                command_number=TMCLCommand.GIO,
                type_number=port_number,
                motor_number=bank_number,
                value=0,
            )
        )

    async def set_user_variable(self, variable_number: int, value: int) -> TmclReply:
        return await self._send_message(
            TmclMessage(
                module_address=self._module_id,
                command_number=TMCLCommand.SGP,
                type_number=variable_number,
                motor_number=2,
                value=value,
            )
        )

    async def get_user_variable(self, variable_number: int) -> TmclReply:
        return await self._send_message(
            TmclMessage(
                module_address=self._module_id,
                command_number=TMCLCommand.SGP,
                type_number=variable_number,
                motor_number=2,
                value=0,
            )
        )
