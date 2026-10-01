from __future__ import annotations
from dataclasses import dataclass
from typing import Tuple


TwoInt = Tuple[int, int]


@dataclass
class AxisCoordinate:
    x: float
    y: float
    s: int = 0  # 0 -> front module, 1 -> back module
    z_offset: float = 0.0

    def __post_init__(self) -> None:
        if self.s not in (0, 1):
            raise ValueError("AxisCoordinate.s must be 0 (front) or 1 (back)")

    def _assert_same_side(self, other: "AxisCoordinate", op: str) -> None:
        if not isinstance(other, AxisCoordinate):
            raise TypeError(f"Cannot {op} AxisCoordinate with {type(other)!r}")
        if self.s != other.s:
            raise ValueError(
                f"Cannot {op} AxisCoordinate instances from different sides "
                f"(self={self.s}, other={other.s})"
            )

    def __add__(self, other: "AxisCoordinate") -> "AxisCoordinate":
        self._assert_same_side(other, "add")
        return AxisCoordinate(
            x=self.x + other.x,
            y=self.y + other.y,
            s=self.s,
            z_offset=self.z_offset + other.z_offset,
        )

    def __sub__(self, other: "AxisCoordinate") -> "AxisCoordinate":
        self._assert_same_side(other, "subtract")
        return AxisCoordinate(
            x=self.x - other.x,
            y=self.y - other.y,
            s=self.s,
            z_offset=self.z_offset - other.z_offset,
        )
