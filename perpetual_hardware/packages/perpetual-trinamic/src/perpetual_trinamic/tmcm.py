# from dataclasses import dataclass
# from enum import IntEnum
from can import BusABC, Message, BufferedReader
from typing import Optional
from perpetual_trinamic.tmcl import TMCLCommand, TmclMessage, TmclReply, MoveType
import struct
import time


# @dataclass
# class TmclMessage:
#    module_address: int
#    command_number: int
#    type_number: int
#    motor_number: int
#    value: int
#
#
# @dataclass
# class TmclReply:
#    module_address: int
#    command_number: int
#    status: int
#    value: int
#
#
# class MoveType(IntEnum):
#    ABSOLUTE = 0
#    RELATIVE = 1
#    COORDINATE = 2


# This Should Never Be Done but Comms are not reliable right now
# def retry(times) -> None:
#    """
#    Retry Decorator
#    Retries the wrapped function/method `times` times if the exceptions listed
#    in ``exceptions`` are thrown
#    :param times: The number of times to repeat the wrapped function/method
#    :type times: Int
#    :param Exceptions: Lists of exceptions that trigger a retry attempt
#    :type Exceptions: Tuple of Exceptions
#    """
#
#    def decorator(func):
#        def newfn(*args, **kwargs):
#            attempt = 0
#            while attempt < times:
#                try:
#                    return func(*args, **kwargs)
#                except Exception as e:
#                    print(
#                        "Exception %s thrown when attempting to run %s, attempt "
#                        "%d of %d" % (func, attempt, times, e)
#                    )
#                    attempt += 1
#            return func(*args, **kwargs)
#
#        return newfn
#
#    return decorator


class TMCM:
    def __init__(
        self,
        connection: BusABC,
        reader: Optional[BufferedReader] = None,
        module_id: int = 1,
        motor_id: int = 0,
    ) -> None:
        self._connection = connection
        self._reader = reader
        self._module_id = module_id & 0xFF
        self._motor_id = motor_id & 0xFF
        self._sync_read = True

    def _send_raw_message(self, message: Message) -> None:
        self._connection.send(message)
        # return self._connection.recv(timeout=1.0)

    def _recv_raw_message(self) -> Optional[Message]:
        return self._connection.recv(timeout=1)

    def _send_message(self, message: TmclMessage) -> TmclReply:
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
        return self._receive_message()

    def _receive_message(self) -> TmclReply:
        if self._reader:
            print("Reader")
            t = time.time()
            while time.time() - t < 2.0:
                msg = self._reader.get_message(timeout=2.0)
                if msg:
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
                    raise Exception(f"No Response From Module {self._module_id}")
            raise Exception(f"No Response From Module {self._module_id}")
        else:
            """
            msg = self._connection.recv(timeout=2.0)
            if msg:
                if len(msg.data) == 7:
                    return TmclReply(
                        module_address=msg.data[0],
                        status=msg.data[1],
                        command_number=msg.data[2],
                        value=struct.unpack(">i", msg.data[3:])[0],
                    )
                else:
                    raise Exception(f"Invalid Data Format: {msg}")
            msg = self._connection.recv(timeout=2.0)
            if msg:
                if len(msg.data) == 7:
                    return TmclReply(
                        module_address=msg.data[0],
                        status=msg.data[1],
                        command_number=msg.data[2],
                        value=struct.unpack(">i", msg.data[3:])[0],
                    )
                else:
                    raise Exception(f"Invalid Data Format: {msg}")
            raise Exception(f"No Response From Module {self._module_id}")
            """
            # return None
            t0 = time.time()
            while time.time() - t0 < 2:
                msg = self._connection.recv(1.0)
                if msg and msg.arbitration_id == self._module_id:
                    if len(msg.data) == 7:
                        return TmclReply(
                            module_address=msg.data[0],
                            status=msg.data[1],
                            command_number=msg.data[2],
                            value=struct.unpack(">i", msg.data[3:])[0],
                        )
                    else:
                        raise Exception(f"Invalid Data Format: {msg}")

            raise Exception(f"Module Failed to Receive Message: {self._module_id}")

    def _default_message(self, command_number: int, type_number: int, value: int) -> TmclMessage:
        return TmclMessage(
            module_address=self._module_id,
            command_number=command_number,
            type_number=type_number,
            motor_number=self._motor_id,
            value=value,
        )

    def rotate_right(self, velocity: int) -> TmclReply:
        return self._send_message(self._default_message(TMCLCommand.ROR, 0, velocity))

    def rotate_left(self, velocity: int) -> TmclReply:
        return self._send_message(self._default_message(TMCLCommand.ROL, 0, velocity))

    def stop(self) -> TmclReply:
        return self._send_message(self._default_message(TMCLCommand.MST, 0, 0))

    def move(self, type: MoveType, value: int) -> TmclReply:
        return self._send_message(self._default_message(TMCLCommand.MVP, type.value, value))

    def move_absolute(self, value: int) -> TmclReply:
        return self._send_message(self._default_message(TMCLCommand.MVP, MoveType.ABSOLUTE, value))

    def move_relative(self, value: int) -> TmclReply:
        return self._send_message(self._default_message(TMCLCommand.MVP, MoveType.RELATIVE, value))

    def set_axis_parameter(self, axis_parameter: int, value: int) -> TmclReply:
        return self._send_message(self._default_message(TMCLCommand.SAP, axis_parameter, value))

    def set_io(self, type_number: int, value: int, bank_number: int = 0) -> TmclReply:
        return self._send_message(
            TmclMessage(
                module_address=self._module_id,
                command_number=TMCLCommand.SIO,
                type_number=type_number,
                motor_number=bank_number,
                value=value,
            )
        )

    def set_global_parameter(self, parameter: int, value: int) -> TmclReply:
        return self._send_message(self._default_message(TMCLCommand.SGP, parameter, value))

    def get_axis_parameter(self, axis_parameter: int) -> TmclReply:
        return self._send_message(self._default_message(TMCLCommand.GAP, axis_parameter, 0))

    def get_digital_input(self, port_number: int, bank_number: int) -> TmclReply:
        return self._send_message(
            TmclMessage(
                module_address=self._module_id,
                command_number=TMCLCommand.GIO,
                type_number=port_number,
                motor_number=bank_number,
                value=0,
            )
        )

    def set_user_variable(self, variable_number: int, value: int) -> TmclReply:
        return self._send_message(
            TmclMessage(
                module_address=self._module_id,
                command_number=TMCLCommand.SGP,
                type_number=variable_number,
                motor_number=2,
                value=value,
            )
        )

    def get_user_variable(self, variable_number: int) -> TmclReply:
        return self._send_message(
            TmclMessage(
                module_address=self._module_id,
                command_number=TMCLCommand.SGP,
                type_number=variable_number,
                motor_number=2,
                value=0,
            )
        )
