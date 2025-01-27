from perpetual_trinamic.tmcl import (
    TMCLCommand,
    TMCLStatus,
    TmclMessage,
    TmclReply,
    MoveType,
    TmclCommandLoaded,
    TmclWrongChecksum,
    TmclInvalidCommand,
    TmclWrongType,
    TmclInvalidValue,
    TmclEepromLocked,
    TmclCommandNotAvailable,
    UnknownTmclResponse,
)
from perpetual_trinamic.base import TrinamicTransporter


class TMCM:
    def __init__(
        self,
        connection: TrinamicTransporter,
        module_id: int = 1,
        motor_id: int = 0,
    ) -> None:
        self._connection = connection
        self._module_id = module_id & 0xFF
        self._motor_id = motor_id & 0xFF

    async def _send_message(self, message: TmclMessage) -> TmclReply:
        reply = await self._connection.send_message(message, 1.0)
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
