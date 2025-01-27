from can import AsyncBufferedReader, BusABC, Message, Notifier
from perpetual_trinamic.tmcl import TMCLCommand, TMCLStatus, TmclMessage, TmclReply, MoveType
import struct
import asyncio
from perpetual_can import CANBusHandler, AsyncFilteredReader


class TmclCommandLoaded(Exception):
    """Tmcl Command Loaded"""

    def __init__(self, value: TmclReply) -> None:
        self._value = value

    def __str__(self) -> str:
        return f"{self.__class__.__name__} {self._value}"


class TmclWrongChecksum(Exception):
    """Tmcl Wrong Checksum"""

    def __init__(self, value: TmclReply) -> None:
        self._value = value

    def __str__(self) -> str:
        return f"{self.__class__.__name__} {self._value}"


class TmclInvalidCommand(Exception):
    """Tmcl Invalid Command"""

    def __init__(self, value: TmclReply) -> None:
        self._value = value

    def __str__(self) -> str:
        return f"{self.__class__.__name__} {self._value}"


class TmclWrongType(Exception):
    """Tmcl Wrong Type"""

    def __init__(self, value: TmclReply) -> None:
        self._value = value

    def __str__(self) -> str:
        return f"{self.__class__.__name__} {self._value}"


class TmclInvalidValue(Exception):
    """Tmcl Invalid Value"""

    def __init__(self, value: TmclReply) -> None:
        self._value = value

    def __str__(self) -> str:
        return f"{self.__class__.__name__} {self._value}"


class TmclEepromLocked(Exception):
    """Tmcl Eeprom Locked"""

    def __init__(self, value: TmclReply) -> None:
        self._value = value

    def __str__(self) -> str:
        return f"{self.__class__.__name__} {self._value}"


class TmclCommandNotAvailable(Exception):
    """Tmcl Command Not Available"""

    def __init__(self, value: TmclReply) -> None:
        self._value = value

    def __str__(self) -> str:
        return f"{self.__class__.__name__} {self._value}"


class UnknownTmclResponse(Exception):
    """Tmcl Unknown Response"""

    def __init__(self, value: TmclReply) -> None:
        self._value = value

    def __str__(self) -> str:
        return f"{self.__class__.__name__} {self._value}"


class TmclInvalidChecksum(Exception):
    """Tmcl Invalid Checksum in Response"""

    def __init__(self, value: TmclReply) -> None:
        self._value = value

    def __str__(self) -> str:
        return f"{self.__class__.__name__} {self._value}"


class TMCM:
    def __init__(
        self,
        connection: CANBusHandler,
        module_id: int = 1,
        motor_id: int = 0,
    ) -> None:
        self._bus = connection
        self._reader = AsyncFilteredReader([module_id])
        self._bus.add_listener(self._reader)
        self._module_id = module_id & 0xFF
        self._motor_id = motor_id & 0xFF

    async def _send_raw_message(self, message: Message) -> None:
        await self._bus.send_message(message)

    async def _recv_raw_message(self) -> Message:
        return await self._reader.get_message()

    async def _send_exclusive_ack_message(self, message: TmclMessage) -> TmclReply:
        msg = await self._bus.send_ack_message(message.into_can_message(), self._reader, 1.0)
        if msg.arbitration_id == self._module_id:
            if msg.is_error_frame:
                raise Exception(f"Encountered Error Frame on {self._module_id}: {msg}")
            if len(msg.data) == 7:
                reply = TmclReply(
                    module_address=msg.data[0],
                    status=msg.data[1],
                    command_number=msg.data[2],
                    value=struct.unpack(">i", msg.data[3:])[0],
                )
                match reply.status:
                    case TMCLStatus.SUCCESS:
                        return reply
                    case TMCLStatus.COMMAND_LOADED:
                        raise TmclCommandLoaded(reply)
                    case TMCLStatus.WRONG_CHECKSUM:
                        raise TmclWrongChecksum(reply)
                    case TMCLStatus.INVALID_COMMAND:
                        raise TmclInvalidCommand(reply)
                    case TMCLStatus.WRONG_TYPE:
                        raise TmclWrongType(reply)
                    case TMCLStatus.INVALID_VALUE:
                        raise TmclInvalidValue(reply)
                    case TMCLStatus.EEPROM_LOCKED:
                        raise TmclEepromLocked(reply)
                    case TMCLStatus.COMMAND_NOT_AVAILABLE:
                        raise TmclCommandNotAvailable(reply)
                    case _:
                        raise UnknownTmclResponse(reply)
            else:
                raise Exception(f"Invalid Data Format: {msg}")
        else:
            raise Exception(f"Invalid Module Response {self._module_id}")

    async def _send_message(self, message: TmclMessage) -> TmclReply:
        response = await self._send_exclusive_ack_message(message)
        """
        payload = [message.command_number, message.type_number, message.motor_number]
        payload.extend(list(struct.pack(">i", message.value)))
        await self._bus.send_message(
            Message(
                arbitration_id=int(message.module_address),
                data=payload,
                is_extended_id=False,
            )
        )
        reply = await self._receive_message()
        match reply.status:
            case TMCLStatus.SUCCESS:
                return reply
            case TMCLStatus.COMMAND_LOADED:
                raise TmclCommandLoaded(reply)
            case TMCLStatus.WRONG_CHECKSUM:
                raise TmclWrongChecksum(reply)
            case TMCLStatus.INVALID_COMMAND:
                raise TmclInvalidCommand(reply)
            case TMCLStatus.WRONG_TYPE:
                raise TmclWrongType(reply)
            case TMCLStatus.INVALID_VALUE:
                raise TmclInvalidValue(reply)
            case TMCLStatus.EEPROM_LOCKED:
                raise TmclEepromLocked(reply)
            case TMCLStatus.COMMAND_NOT_AVAILABLE:
                raise TmclCommandNotAvailable(reply)
            case _:
                raise UnknownTmclResponse(reply)
        """
        return response

    async def _receive_message(self) -> TmclReply:
        msg = await self._reader.get_message(2.0)
        if msg.arbitration_id == self._module_id:
            if msg.is_error_frame:
                raise Exception(f"Encountered Error Frame on {self._module_id}: {msg}")
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
        print(variable_number, value)
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
        print(variable_number)
        return await self._send_message(
            TmclMessage(
                module_address=self._module_id,
                command_number=TMCLCommand.SGP,
                type_number=variable_number,
                motor_number=2,
                value=0,
            )
        )
