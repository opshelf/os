# --- Utility functions ---
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
