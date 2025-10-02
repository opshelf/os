"""Lightweight storage models retained for compatibility with server APIs and tests."""
from __future__ import annotations

from typing import Any, Dict

from pydantic import BaseModel

Json = Dict[str, Any]


class Dimensions(BaseModel):
    width: float
    height: float
    length: float

    def __getitem__(self, item: str) -> float:
        return float(getattr(self, item))
