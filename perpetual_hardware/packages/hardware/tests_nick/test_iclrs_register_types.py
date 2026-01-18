'''
Tests for iclrs_register_types.py
'''

import pytest
from hardware.drivers.iclrs_register_types import (
    ControlWordFunction,
    DigitalInputFilter,
    DigitalInputFunction,
    DigitalInputOutputMode,
    DigitalInputSettings,
    DigitalOutputFunction,
    DigitalOutputSettings,
)


@pytest.mark.parametrize("function, mode, filter, expected", [
    (DigitalInputFunction.ENABLE, DigitalInputOutputMode.NORMALLY_CLOSED, DigitalInputFilter.FILTER_50_MS, 3208),
    (DigitalInputFunction.ENABLE, DigitalInputOutputMode.NORMALLY_CLOSED, DigitalInputFilter.FILTER_10_MS, 136),
    (DigitalInputFunction.JOG_2, DigitalInputOutputMode.NORMALLY_OPENED, DigitalInputFilter.FILTER_500_MS, 3884)
])
def test_digital_input_settings_pack_fields(function, mode, filter, expected):
    '''
    Tests that DigitalInputSettings packs settings fields into value for
    writing to the motor controller.
    Test parameters are from examples in the datasheet (p. 18).
    '''
    digital_input_setting = DigitalInputSettings(
        function = function,
        mode = mode,
        filter = filter)
    assert digital_input_setting.value == expected

@pytest.mark.parametrize("value, expected", [
    (3208, DigitalInputFunction.ENABLE),
    (136, DigitalInputFunction.ENABLE),
    (3884, DigitalInputFunction.JOG_2)
])
def test_digital_input_settings_parse_function(value, expected):
    '''
    Tests that DigitalInputSettings can parse a value into function setting.
    Test parameters are from the inverse of the examples in the datasheet (p. 18).
    '''
    digital_input_setting = DigitalInputSettings(value = value)
    assert digital_input_setting.function == expected

@pytest.mark.parametrize("value, expected", [
    (3208, DigitalInputOutputMode.NORMALLY_CLOSED),
    (136, DigitalInputOutputMode.NORMALLY_CLOSED),
    (3884, DigitalInputOutputMode.NORMALLY_OPENED)
])
def test_digital_input_settings_parse_mode(value, expected):
    '''
    Tests that DigitalInputSettings can parse a value into mode setting.
    Test parameters are from the inverse of the examples in the datasheet (p. 18).
    '''
    digital_input_setting = DigitalInputSettings(value = value)
    assert digital_input_setting.mode == expected

@pytest.mark.parametrize("value, expected", [
    (3208, DigitalInputFilter.FILTER_50_MS),
    (136, DigitalInputFilter.FILTER_10_MS),
    (3884, DigitalInputFilter.FILTER_500_MS)
])
def test_digital_input_settings_parse_filter(value, expected):
    '''
    Tests that DigitalInputSettings can parse a value into filter setting.
    Test parameters are from the inverse of the examples in the datasheet (p. 18).
    '''
    digital_input_setting = DigitalInputSettings(value = value)
    assert digital_input_setting.filter == expected

@pytest.mark.parametrize("function, mode, expected", [
    (DigitalOutputFunction.COMMAND_COMPLETE, DigitalInputOutputMode.NORMALLY_OPENED, 0x20),
    (DigitalOutputFunction.PATH_COMPLETE, DigitalInputOutputMode.NORMALLY_OPENED, 0x21),
    (DigitalOutputFunction.HOMING_COMPLETE, DigitalInputOutputMode.NORMALLY_OPENED, 0x22),
    (DigitalOutputFunction.ALARM, DigitalInputOutputMode.NORMALLY_OPENED, 0x25),
    (DigitalOutputFunction.BRAKE, DigitalInputOutputMode.NORMALLY_OPENED, 0x24),
    (DigitalOutputFunction.IN_POSITION, DigitalInputOutputMode.NORMALLY_OPENED, 0x23),
    (DigitalOutputFunction.COMMAND_COMPLETE, DigitalInputOutputMode.NORMALLY_CLOSED, 0xA0),
    (DigitalOutputFunction.PATH_COMPLETE, DigitalInputOutputMode.NORMALLY_CLOSED, 0xA1),
    (DigitalOutputFunction.HOMING_COMPLETE, DigitalInputOutputMode.NORMALLY_CLOSED, 0xA2),
    (DigitalOutputFunction.ALARM, DigitalInputOutputMode.NORMALLY_CLOSED, 0xA5),
    (DigitalOutputFunction.BRAKE, DigitalInputOutputMode.NORMALLY_CLOSED, 0xA4),
    (DigitalOutputFunction.IN_POSITION, DigitalInputOutputMode.NORMALLY_CLOSED, 0xA3),

])

def test_digital_output_settings_pack_fields(function, mode, expected):
    '''
    Tests that DigitalOutputSettings packs settings fields into value for
    writing to the motor controller.
    Test parameters are from examples in the datasheet (p. 16),
    covers all possible settings.
    '''
    digital_output_setting = DigitalOutputSettings(
        function = function,
        mode = mode)
    assert digital_output_setting.value == expected

@pytest.mark.parametrize("value, expected", [
    (0x20, DigitalOutputFunction.COMMAND_COMPLETE),
    (0x21, DigitalOutputFunction.PATH_COMPLETE),
    (0x22, DigitalOutputFunction.HOMING_COMPLETE),
    (0x25, DigitalOutputFunction.ALARM),
    (0x24, DigitalOutputFunction.BRAKE),
    (0x23, DigitalOutputFunction.IN_POSITION),
    (0xA0, DigitalOutputFunction.COMMAND_COMPLETE),
    (0xA1, DigitalOutputFunction.PATH_COMPLETE),
    (0xA2, DigitalOutputFunction.HOMING_COMPLETE),
    (0xA5, DigitalOutputFunction.ALARM),
    (0xA4, DigitalOutputFunction.BRAKE),
    (0xA3, DigitalOutputFunction.IN_POSITION)
])
def test_digital_output_settings_parse_function(value, expected):
    '''
    Tests that DigitalOutputSettings parses pin function from value.
    Test parameters are from inverse examples in the datasheet (p. 16),
    covers all possible settings.
    '''
    digital_output_setting = DigitalOutputSettings(value = value)
    assert digital_output_setting.function == expected

@pytest.mark.parametrize("value, expected", [
    (0x20, DigitalInputOutputMode.NORMALLY_OPENED),
    (0x21, DigitalInputOutputMode.NORMALLY_OPENED),
    (0x22, DigitalInputOutputMode.NORMALLY_OPENED),
    (0x25, DigitalInputOutputMode.NORMALLY_OPENED),
    (0x24, DigitalInputOutputMode.NORMALLY_OPENED),
    (0x23, DigitalInputOutputMode.NORMALLY_OPENED),
    (0xA0, DigitalInputOutputMode.NORMALLY_CLOSED),
    (0xA1, DigitalInputOutputMode.NORMALLY_CLOSED),
    (0xA2, DigitalInputOutputMode.NORMALLY_CLOSED),
    (0xA5, DigitalInputOutputMode.NORMALLY_CLOSED),
    (0xA4, DigitalInputOutputMode.NORMALLY_CLOSED),
    (0xA3, DigitalInputOutputMode.NORMALLY_CLOSED)
])
def test_digital_output_settings_parse_mode(value, expected):
    '''
    Tests that DigitalOutputSettings parses pin mode from value.
    Test parameters are from inverse examples in the datasheet (p. 16),
    covers all possible settings.
    '''
    digital_output_setting = DigitalOutputSettings(value = value)
    assert digital_output_setting.mode == expected

@pytest.mark.parametrize("function, expected", [
    (ControlWordFunction.RESET_CURRENT_ALARM, 0x1111),
    (ControlWordFunction.RESET_HISTORY_ALARM, 0x1122),
    (ControlWordFunction.SAVE_ALL_PARAMETERS_TO_EEPROM, 0x2211),
    (ControlWordFunction.RESET_PARAMETERS_EXCLUDE_MOTOR_PARAMETERS, 0X2222),
    (ControlWordFunction.RESET_ALL_PARAMETERS, 0X2233),
    (ControlWordFunction.SAVE_ALL_MAPPINGS_INTO_EEPROM, 0X2244),
    (ControlWordFunction.JOG_CW, 0X4001),
    (ControlWordFunction.JOG_CCW, 0X4002)
])
def test_control_word(function, expected):
    assert function.value == expected