"""Shared lightweight types used across the OS1 server without depending on legacy hardware packages."""
from __future__ import annotations

from typing import Any, Dict

from pydantic import BaseModel

Json = Dict[str, Any]


class Dimensions(BaseModel):
    length: float
    width: float
    height: float
