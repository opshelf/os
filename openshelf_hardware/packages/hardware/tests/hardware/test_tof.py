import os

import pytest

from hardware.tof import ToFConnectionError, ToFSensorBus


class FakeSensor:
    data_ready = True
    distance = 123

    def clear_interrupt(self) -> None:
        self.cleared = True


def test_tof_bus_reads_all_configured_channels() -> None:
    bus = ToFSensorBus(
        ftdi_url="ftdi://ftdi:232h/1",
        mux_address=112,
        sensor_address=41,
        channels=[6, 7],
        timing_budget_ms=200,
        read_timeout_s=0.1,
        sensor_factory=lambda _channel, _address: FakeSensor(),
    )

    bus.connect()
    readings = bus.read_all_distances_mm()

    assert [reading.channel for reading in readings] == [6, 7]
    assert [reading.distance_mm for reading in readings] == [123.0, 123.0]


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


class FakeI2CController:
    def __init__(self) -> None:
        self.did_close = False
        self.did_terminate = False

    def close(self) -> None:
        self.did_close = True

    def terminate(self) -> None:
        self.did_terminate = True


class FakeI2C:
    def __init__(self, *, has_deinit: bool = False, has_nested_controller: bool = False) -> None:
        self.did_deinit = False
        self._has_deinit = has_deinit
        self._i2c = FakeI2CController() if has_nested_controller is True else None

    def __getattr__(self, name: str):
        if name != "deinit" or self._has_deinit is not True:
            raise AttributeError(name)

        def did_deinit() -> None:
            self.did_deinit = True

        return did_deinit


class FakeBlinkaI2C(FakeI2C):
    def __getattr__(self, name: str):
        if name != "deinit" or self._has_deinit is not True:
            raise AttributeError(name)

        def did_deinit() -> None:
            self.did_deinit = True
            del self._i2c

        return did_deinit


def test_tof_bus_reads_adafruit_sensor_distances_in_mm_and_sets_ftdi_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_sensor = FakeAdafruitSensor()
    did_create_i2c = False

    monkeypatch.setenv("BLINKA_FT232H", "ftdi://previous")

    def did_import_module(name: str):
        if name == "board":

            class BoardModule:
                SCL = object()
                SDA = object()

            return BoardModule

        if name == "busio":

            class BusIOModule:
                class I2C:
                    def __init__(self, scl: object, sda: object) -> None:
                        nonlocal did_create_i2c
                        did_create_i2c = True
                        assert os.environ["BLINKA_FT232H"] == "ftdi://configured"
                        self.scl = scl
                        self.sda = sda

            return BusIOModule

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


def test_tof_bus_stops_started_sensors_when_adafruit_connect_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_sensor = FakeAdafruitSensor()
    created_i2c = FakeI2C(has_deinit=True)
    sensor_calls = 0

    def did_import_module(name: str):
        if name == "board":

            class BoardModule:
                SCL = object()
                SDA = object()

            return BoardModule

        if name == "busio":

            class BusIOModule:
                class I2C:
                    def __new__(cls, scl: object, sda: object) -> FakeI2C:
                        return created_i2c

            return BusIOModule

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
                    nonlocal sensor_calls
                    assert address == 41
                    sensor_calls += 1
                    if sensor_calls == 1:
                        return first_sensor
                    raise RuntimeError("sensor init failed")

            return Vl53Module

        raise AssertionError(f"unexpected module import: {name}")

    monkeypatch.setattr("hardware.tof.import_module", did_import_module)

    bus = ToFSensorBus(
        ftdi_url="ftdi://ftdi:232h/1",
        mux_address=112,
        sensor_address=41,
        channels=[6, 7],
        timing_budget_ms=200,
        read_timeout_s=0.1,
    )

    with pytest.raises(ToFConnectionError, match="sensor init failed"):
        bus.connect()

    assert first_sensor.did_stop_ranging is True
    assert created_i2c.did_deinit is True
    assert bus._i2c is None
    assert bus._sensors == []


def test_tof_bus_closes_i2c_when_sensor_stop_fails_on_connect_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_sensor = FakeAdafruitSensor(should_fail_on_stop=True)
    created_i2c = FakeI2C(has_deinit=True)
    sensor_calls = 0

    def did_import_module(name: str):
        if name == "board":

            class BoardModule:
                SCL = object()
                SDA = object()

            return BoardModule

        if name == "busio":

            class BusIOModule:
                class I2C:
                    def __new__(cls, scl: object, sda: object) -> FakeI2C:
                        return created_i2c

            return BusIOModule

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
                    nonlocal sensor_calls
                    assert address == 41
                    sensor_calls += 1
                    if sensor_calls == 1:
                        return first_sensor
                    raise RuntimeError("sensor init failed")

            return Vl53Module

        raise AssertionError(f"unexpected module import: {name}")

    monkeypatch.setattr("hardware.tof.import_module", did_import_module)

    bus = ToFSensorBus(
        ftdi_url="ftdi://ftdi:232h/1",
        mux_address=112,
        sensor_address=41,
        channels=[6, 7],
        timing_budget_ms=200,
        read_timeout_s=0.1,
    )

    with pytest.raises(ToFConnectionError, match="sensor init failed"):
        bus.connect()

    assert first_sensor.did_stop_ranging is True
    assert created_i2c.did_deinit is True
    assert bus._i2c is None
    assert bus._sensors == []


def test_tof_bus_closes_nested_i2c_controller_when_closed() -> None:
    bus = ToFSensorBus(
        ftdi_url="ftdi://ftdi:232h/1",
        mux_address=112,
        sensor_address=41,
        channels=[6],
        timing_budget_ms=200,
        read_timeout_s=0.1,
        sensor_factory=lambda _channel, _address: FakeSensor(),
    )
    i2c = FakeI2C(has_nested_controller=True)
    bus._i2c = i2c
    bus._mux = FakeMux()

    bus.close()

    assert i2c._i2c is not None
    assert i2c._i2c.did_close is True
    assert bus._i2c is None
    assert bus._mux is None


def test_tof_bus_closes_nested_i2c_controller_before_deinit() -> None:
    bus = ToFSensorBus(
        ftdi_url="ftdi://ftdi:232h/1",
        mux_address=112,
        sensor_address=41,
        channels=[6],
        timing_budget_ms=200,
        read_timeout_s=0.1,
        sensor_factory=lambda _channel, _address: FakeSensor(),
    )
    i2c = FakeBlinkaI2C(has_deinit=True, has_nested_controller=True)
    controller = i2c._i2c
    bus._i2c = i2c
    bus._mux = FakeMux()

    bus.close()

    assert controller.did_close is True
    assert i2c.did_deinit is True
    assert bus._i2c is None
    assert bus._mux is None


def test_tof_bus_closes_i2c_when_sensor_stop_fails_on_close() -> None:
    bus = ToFSensorBus(
        ftdi_url="ftdi://ftdi:232h/1",
        mux_address=112,
        sensor_address=41,
        channels=[6],
        timing_budget_ms=200,
        read_timeout_s=0.1,
        sensor_factory=lambda _channel, _address: FakeSensor(),
    )
    i2c = FakeI2C(has_nested_controller=True)
    bus._i2c = i2c
    bus._mux = FakeMux()
    bus._sensors = [FakeAdafruitSensor(should_fail_on_stop=True)]
    bus._distances_are_cm = True

    with pytest.raises(RuntimeError, match="stop failed"):
        bus.close()

    assert i2c._i2c is not None
    assert i2c._i2c.did_close is True
    assert bus._i2c is None
    assert bus._mux is None
    assert bus._sensors == []
    assert bus._distances_are_cm is False
