import pytest
from hardware.drivers.utilty_functions import (
    coerce_str_to_int,
    create_map_of_names_to_enum_members,
    extract_bits,
    find_enums_in_class_type_hints,
    pack_bits,
)

# --- Test extract_bits ---
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


# --- Test pack_bits ---
bits_data = [
    ([(1, 0, 1)], 0b1),
    ([(1, 1, 1)], 0b10),
    ([(1, 0, 1), (1, 1, 1)], 0b11),
    ([(0, 0, 1), (1, 1, 1)], 0b10),
    ([(0b11, 0, 2)], 0b11),
    ([(0b11, 0, 2), (0b101, 2, 3)], 0b10111),
]


@pytest.mark.parametrize("bits, expected_value", bits_data)
def test_pack_bits(bits, expected_value):
    value = pack_bits(bits)
    assert value == expected_value


# --- Test coerce_str_to_int ---
str_int_data = [
    ("0b0", 0),
    ("0B0", 0),
    ("0b1", 1),
    ("0B1", 1),
    ("0b11", 0b11),
    ("0B11", 0b11),
    ("0xDEADBEEF", 0xDEADBEEF),
    ("0XDEADBEEF", 0xDEADBEEF),
    ("0xFAIL", ValueError),
    ("FAIL", ValueError),
]


@pytest.mark.parametrize(
    "value_str, expected_value_int", str_int_data
)
def test_coerce_str_to_int(value_str, expected_value_int):
    try:
        value = coerce_str_to_int(value_str)
    except ValueError as e:
        value = type(e)
    assert value == expected_value_int


# --- Test find_enums_in_class_type_hints ---
def test_find_enums_in_class_type_hints():
    from enum import IntEnum

    class Color(IntEnum):
        RED = 1
        GREEN = 2
        BLUE = 3

    class Size(IntEnum):
        SMALL = 1
        MEDIUM = 2
        LARGE = 3

    class TestClass:
        color: Color
        size: Size
        value: int

    enums_found = find_enums_in_class_type_hints(TestClass)
    assert set(enums_found) == {Color, Size}


# --- Test create_map_of_names_to_enum_members ---
def test_create_map_of_names_to_enum_members():
    from enum import IntEnum

    class Color(IntEnum):
        RED = 1
        GREEN = 2
        BLUE = 3

    expected_mapping = {
        "Color.RED": Color.RED,
        "Color.GREEN": Color.GREEN,
        "Color.BLUE": Color.BLUE,
    }
    mapping = create_map_of_names_to_enum_members(Color)
    assert mapping == expected_mapping


# --- Test create_clamped_value_model ---
def test_clamped_value_model():
    from hardware.drivers.utilty_functions import (
        create_clamped_value_model,
    )

    ClampedValueModel = create_clamped_value_model(
        name="TestClampedValueModel",
        min=0,
        max=100,
    )

    # Test within bounds
    model = ClampedValueModel(value=50)
    assert model.clamped_value == 50

    # Test below min
    model = ClampedValueModel(value=-10)
    assert model.clamped_value == 0

    # Test above max
    model = ClampedValueModel(value=150)
    assert model.clamped_value == 100
