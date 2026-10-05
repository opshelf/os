import os

import pytest

from hardware.tof import ToFSensorBus


class FakeAdafruitSensor:
    data_ready = True
    distance = 12.3

    def __init__(self, *, should_fail_on_stop: bool = False) -> None:
        self.timing_budget: int | None = None
        self.distance_mode: int | None = None
        self.roi_xy: tuple[int, int] | None = None
        self.roi_center: int | None = None
        self.did_stop_ranging = False
        self.did_clear_interrupt = False
        self.should_fail_on_stop = should_fail_on_stop

    def start_ranging(self) -> None:
        return None

    def stop_ranging(self) -> None:
        self.did_stop_ranging = True
        if self.should_fail_on_stop is True:
            raise RuntimeError("stop failed")

    def clear_interrupt(self) -> None:
        self.did_clear_interrupt = True


class FakeMux:
    def __getitem__(self, channel: int) -> int:
        return channel


def test_tof_bus_reads_adafruit_sensor_distances_in_mm_and_sets_ftdi_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_sensor = FakeAdafruitSensor()
    did_create_i2c = False

    monkeypatch.setenv("BLINKA_FT232H", "ftdi://previous")

    def did_import_module(name: str):
        if name == "adafruit_blinka.microcontroller.ftdi_mpsse.mpsse.i2c":

            class FtdiI2CModule:
                class I2C:
                    def __init__(self) -> None:
                        nonlocal did_create_i2c
                        did_create_i2c = True
                        assert os.environ["BLINKA_FT232H"] == "ftdi://configured"

            return FtdiI2CModule

        if name == "adafruit_tca9548a":

            class TcaModule:
                @staticmethod
                def TCA9548A(_i2c: object, address: int) -> FakeMux:
                    assert address == 112
                    return FakeMux()

            return TcaModule

        if name == "adafruit_vl53l1x":

            class Vl53Module:
                @staticmethod
                def VL53L1X(_channel: object, address: int) -> FakeAdafruitSensor:
                    assert address == 41
                    return first_sensor

            return Vl53Module

        raise AssertionError(f"unexpected module import: {name}")

    monkeypatch.setattr("hardware.tof.import_module", did_import_module)

    bus = ToFSensorBus(
        ftdi_url="ftdi://configured",
        mux_address=112,
        sensor_address=41,
        channels=[6],
        timing_budget_ms=200,
        read_timeout_s=0.1,
        distance_mode=1,
        roi_width=4,
        roi_height=4,
        roi_center=199,
    )

    bus.connect()
    reading = bus.read_distance_mm(0)

    assert did_create_i2c is True
    assert os.environ["BLINKA_FT232H"] == "ftdi://previous"
    assert reading.distance_mm == pytest.approx(123.0)
    assert first_sensor.did_clear_interrupt is True
    assert first_sensor.distance_mode == 1
    assert first_sensor.roi_xy == (4, 4)
    assert first_sensor.roi_center == 199
    assert first_sensor.timing_budget == 200


