import importlib.resources
import inspect
from enum import IntEnum
from pathlib import Path
from typing import Any, Dict, Union, get_type_hints

from pydantic import BaseModel, Field, create_model, model_validator


# --- Bit handling functions ---
def extract_bits(value: int, start_bit: int, num_bits: int) -> int:
    """Extracts bits from an integer value.

    Args:
        value (int): The integer value to extract bits from.
        start_bit (int): The starting bit position (0-indexed).
        num_bits (int): The number of bits to extract.

    Returns:
        int: The extracted bits as an integer.
    """
    mask = (1 << num_bits) - 1
    return (value >> start_bit) & mask


def pack_bits(bits: list[tuple[int, int, int]]) -> int:
    """Pack bits into an integer value.

    Args:
        bits (list[tuple[int, int, int]]): The list of bits to pack,
            where a bit is: tuple(value, start_bit, num_bits
    Returns:
        int: The extracted bits as an integer.
    """
    value: int = 0
    for bit in bits:
        bit_value = bit[0]
        start_bit = bit[1]
        num_bits = bit[2]
        mask = (1 << num_bits) - 1
        value = value | ((bit_value & mask) << start_bit)
    return value


# --- Enum processing functions ---
def find_enums_in_class_type_hints(cls):
    type_hints = get_type_hints(cls)
    enums_used = set()
    for name, type_hint in type_hints.items():
        # Check if the type hint is an Enum class or a subclass
        if inspect.isclass(type_hint) and issubclass(
            type_hint, IntEnum
        ):
            enums_used.add(type_hint)
    return list(enums_used)


def create_map_of_names_to_enum_members(
    enum: type[IntEnum],
) -> Dict[str, IntEnum]:
    names = [member.name for member in enum]
    enum_name = enum.__name__
    mapping = {f"{enum_name}.{name}": enum[name] for name in names}
    return mapping


def enum_to_string(value: str) -> str:
    enum_value = (
        repr(value).replace("<", "").replace(">", "").split(":")[0]
    )
    return enum_value


def coerce_str_to_int(v_string: str) -> int:
    """Coerces string representations like "0xFFF" or "0b1111" into equivalent int.
    Usually used when converting from string-based inputs (like csv or json).
    """
    v_string = v_string.lower()
    try:
        return int(v_string)
    except ValueError:
        if v_string.startswith("0x"):
            return int(v_string, 16)
        elif v_string.startswith("0b"):
            return int(v_string, 2)
        else:
            raise ValueError(
                f"literal does not start with '0x' or '0b': '{v_string}'"
            )


# --- Data file access ---
def get_data_path(filename):
    with importlib.resources.path("hardware.data", filename) as p:
        return Path(p)


# --- Data conversion ---
def round_to_nearest(value: float, multiple: float) -> float:
    return round(value / multiple) * multiple
