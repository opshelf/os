from hardware.icl_rs.parameters import (DigitalInputFunction, 
    DigitalInputFilter, DigitalOutputFunction, DigitalInputOutputMode,
    DigitalInputSettings
)

def test_digital_input_settings():
    digital_input_settings = DigitalInputSettings(
        function = DigitalInputFunction.ENABLE,
        mode = DigitalInputOutputMode.NORMALLY_CLOSED,
        filter = DigitalInputFilter.FILTER_50_MS
    )
    expected = 0b000110010001000
    assert digital_input_settings.value == expected
