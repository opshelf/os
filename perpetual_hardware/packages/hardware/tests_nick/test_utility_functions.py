import pytest
from hardware.drivers.utilty_functions import (
    coerce_str_to_int,
    extract_bits,
    pack_bits,
)

value_start_num_data = [
    (0b0, 0, 1, 0),
    (0b1, 0, 1, 1),
    (0b01, 1, 1, 0),
    (0b11, 1, 1, 1),
    (0b11, 0, 2, 0b11),
    (0b110, 1, 2, 0b11),
    (0b10101100, 1, 5, 0b10110),
]


@pytest.mark.parametrize(
    "value, start_bit, num_bits, extracted_expected",
    value_start_num_data,
)
def test_extract_bits(value, start_bit, num_bits, extracted_expected):
    value = extract_bits(value, start_bit, num_bits)
    assert value == extracted_expected


bits_data = [
    ([(1, 0, 1)], 0b1),
    ([(1, 1, 1)], 0b10),
    ([(1, 0, 1), (1, 1, 1)], 0b11),
    ([(0, 0, 1), (1, 1, 1)], 0b10),
    ([(0b11, 0, 2)], 0b11),
    ([(0b11, 0, 2), (0b101, 2, 3)], 0b10111),
]


@pytest.mark.parametrize(
    "bits, expected_value",
    bits_data,
)
def test_pack_bits(bits, expected_value):
    value = pack_bits(bits)
    assert value == expected_value


str_int_data = [
    ("0b0", 0),
    ("0B0", 0),
    ("0b1", 1),
    ("0B1", 1),
    ("0b11", 0b11),
    ("0B11", 0b11),
    ("0xDEADBEEF", 0xDEADBEEF),
    ("0XDEADBEEF", 0xDEADBEEF),
    ("0xFAIL", 0x0),
    ("FAIL", 0x0),
]


@pytest.mark.parametrize(
    "value_str, expected_value_int",
    str_int_data,
)
def test_coerce_str_to_int(value_str, expected_value_int):
    value = coerce_str_to_int(value_str)
    assert value == expected_value_int
