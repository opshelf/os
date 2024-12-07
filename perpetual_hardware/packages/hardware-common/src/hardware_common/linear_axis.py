from abc import ABC, abstractmethod
from enum import Enum


class AxisStatus(Enum):
    ERROR = "ERROR"
    GOOD = "GOOD"


class AbstractAxis(ABC):
    @abstractmethod
    def initialize(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def home(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_position(self) -> float:
        raise NotImplementedError

    @abstractmethod
    def move_to_position(self, position: float) -> None:
        raise NotImplementedError

    @abstractmethod
    def move_to_relative_position(self, position: float) -> None:
        raise NotImplementedError

    @abstractmethod
    def wait_for_move(self, timeout: float = 10) -> None:
        raise NotImplementedError

    @abstractmethod
    def stop_motor(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_status(self) -> AxisStatus:
        raise NotImplementedError

    @abstractmethod
    def set_speed(self, mms_ps: float) -> None:
        raise NotImplementedError

    @abstractmethod
    def set_acceleration(self, mmss: float) -> None:
        raise NotImplementedError


class MaxTravelLimitError(Exception):
    """The Requested Travel Distance Exceeds the Maximum"""


class MinTravelLimitError(Exception):
    """The Requested Travel Distance Exceeds the Minimum"""
