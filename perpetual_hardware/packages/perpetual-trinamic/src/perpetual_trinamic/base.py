from abc import ABCMeta, abstractmethod
from perpetual_trinamic.tmcl import TmclReply, TmclMessage
from typing import Optional


class TrinamicTransporter(metaclass=ABCMeta):
    @abstractmethod
    async def send_message(
        self, message: TmclMessage, timeout: Optional[float] = 1.0
    ) -> TmclReply: ...
