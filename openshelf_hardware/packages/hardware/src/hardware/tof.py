from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from importlib import import_module
from typing import Any, Callable, Protocol

from hardware.exceptions import ToFConfigError, ToFConnectionError, ToFReadError


class DistanceSensor(Protocol):
    @property
    def data_ready(self) -> bool: ...

    @property
    def distance(self) -> int | float | None: ...

    def clear_interrupt(self) -> None: ...


@dataclass(frozen=True)
class ToFReading:
    sensor_index: int
    channel: int
    distance_mm: float


class ToFSensorBus:
    def __init__(
        self,
        *,
        ftdi_url: str,
        mux_address: int,
        sensor_address: int,
        channels: list[int],
        timing_budget_ms: int,
        read_timeout_s: float,
        distance_mode: int | None = None,
        roi_width: int | None = None,
        roi_height: int | None = None,
        roi_center: int | None = None,
        sensor_factory: Callable[[Any, int], DistanceSensor] | None = None,
    ) -> None:
        if not ftdi_url:
            raise ToFConfigError("ftdi_url is required")
        if mux_address < 0 or mux_address > 127 or sensor_address < 0 or sensor_address > 127:
            raise ToFConfigError("I2C addresses must be in range 0..127")
        if len(channels) == 0 or len(set(channels)) != len(channels):
            raise ToFConfigError("channels must be non-empty and unique")
        if any(channel < 0 or channel > 7 for channel in channels):
            raise ToFConfigError("channels must be in range 0..7")
        if timing_budget_ms <= 0 or read_timeout_s <= 0:
            raise ToFConfigError("timing budget and read timeout must be positive")
        if distance_mode is not None and distance_mode not in (1, 2):
            raise ToFConfigError("distance mode must be 1 (short) or 2 (long)")
        if (roi_width is None) != (roi_height is None):
            raise ToFConfigError("roi_width and roi_height must be configured together")
        if roi_width is not None and (roi_width < 1 or roi_width > 16):
            raise ToFConfigError("roi_width must be in range 1..16")
        if roi_height is not None and (roi_height < 1 or roi_height > 16):
            raise ToFConfigError("roi_height must be in range 1..16")
        if roi_center is not None and (roi_center < 0 or roi_center > 255):
            raise ToFConfigError("roi_center must be in range 0..255")

        self.ftdi_url = ftdi_url
        self.mux_address = mux_address
        self.sensor_address = sensor_address
        self.channels = list(channels)
        self.timing_budget_ms = timing_budget_ms
        self.read_timeout_s = read_timeout_s
        self.distance_mode = distance_mode
        self.roi_width = roi_width
        self.roi_height = roi_height
        self.roi_center = roi_center
        self._sensor_factory = sensor_factory
        self._lock = threading.Lock()
        self._i2c: Any | None = None
        self._mux: Any | None = None
        self._sensors: list[DistanceSensor] = []
        self._distances_are_cm = False

    def _close_i2c(self) -> None:
        i2c = self._i2c
        self._i2c = None
        self._mux = None
        if i2c is None:
            return

        first_error = None

        for attribute_name in ("_i2c", "i2c"):
            controller_wrapper = getattr(i2c, attribute_name, None)
            if controller_wrapper is None:
                continue

            for method_name in ("close", "terminate"):
                method = getattr(controller_wrapper, method_name, None)
                if callable(method) is not True:
                    continue

                try:
                    method()
                except Exception as exc:
                    if first_error is None:
                        first_error = exc
                break

        deinit = getattr(i2c, "deinit", None)
        if callable(deinit) is True:
            try:
                deinit()
            except Exception as exc:
                if first_error is None:
                    first_error = exc

        if first_error is not None:
            raise first_error

    def _stop_sensors(self, sensors: list[DistanceSensor]) -> Exception | None:
        first_error = None
        for sensor in sensors:
            stop_ranging = getattr(sensor, "stop_ranging", None)
            if callable(stop_ranging) is not True:
                continue

            try:
                stop_ranging()
            except Exception as exc:
                if first_error is None:
                    first_error = exc

        return first_error

    def connect(self) -> None:
        with self._lock:
            if self._sensors:
                return

            try:
                if self._sensor_factory is None:
                    self._connect_adafruit()
                else:
                    self._distances_are_cm = False
                    self._sensors = [
                        self._sensor_factory(channel, self.sensor_address)
                        for channel in self.channels
                    ]
            except Exception as exc:
                self._distances_are_cm = False
                self._sensors = []
                raise ToFConnectionError(str(exc)) from exc

    def _configure_adafruit_sensor(self, sensor: Any) -> None:
        if self.distance_mode is not None:
            sensor.distance_mode = self.distance_mode

        if self.roi_width is not None and self.roi_height is not None:
            sensor.roi_xy = (self.roi_width, self.roi_height)

        if self.roi_center is not None:
            sensor.roi_center = self.roi_center

        sensor.timing_budget = self.timing_budget_ms

    def _connect_adafruit(self) -> None:
        previous_ftdi_url = os.environ.get("BLINKA_FT232H")
        os.environ["BLINKA_FT232H"] = self.ftdi_url

        try:
            try:
                board = import_module("board")
                busio = import_module("busio")
                adafruit_tca9548a = import_module("adafruit_tca9548a")
                adafruit_vl53l1x = import_module("adafruit_vl53l1x")
            except ImportError as exc:
                raise ToFConnectionError("Adafruit TOF dependencies are not installed") from exc

            sensors: list[DistanceSensor] = []
            try:
                self._i2c = busio.I2C(board.SCL, board.SDA)
                self._mux = adafruit_tca9548a.TCA9548A(self._i2c, address=self.mux_address)

                for channel in self.channels:
                    sensor = adafruit_vl53l1x.VL53L1X(
                        self._mux[channel], address=self.sensor_address
                    )
                    self._configure_adafruit_sensor(sensor)
                    sensor.start_ranging()
                    sensors.append(sensor)
            except Exception:
                try:
                    self._stop_sensors(sensors)
                finally:
                    self._close_i2c()
                raise

            self._distances_are_cm = True
            self._sensors = sensors
        finally:
            if previous_ftdi_url is None:
                os.environ.pop("BLINKA_FT232H", None)
            else:
                os.environ["BLINKA_FT232H"] = previous_ftdi_url

    def close(self) -> None:
        with self._lock:
            stop_error = None
            try:
                stop_error = self._stop_sensors(self._sensors)
            finally:
                self._sensors = []
                try:
                    self._close_i2c()
                finally:
                    self._distances_are_cm = False

            if stop_error is not None:
                raise stop_error

    def read_distance_mm(self, sensor_index: int) -> ToFReading:
        with self._lock:
            if sensor_index < 0 or sensor_index >= len(self._sensors):
                raise ToFReadError(f"sensor index {sensor_index} is out of range")

            sensor = self._sensors[sensor_index]
            deadline = time.monotonic() + self.read_timeout_s

            while time.monotonic() < deadline:
                if sensor.data_ready is True:
                    distance = sensor.distance
                    sensor.clear_interrupt()
                    if distance is None:
                        raise ToFReadError(f"sensor {sensor_index} returned no distance")

                    distance_mm = float(distance)
                    if self._distances_are_cm is True:
                        distance_mm *= 10.0

                    return ToFReading(
                        sensor_index=sensor_index,
                        channel=self.channels[sensor_index],
                        distance_mm=distance_mm,
                    )

                time.sleep(0.005)

        raise ToFReadError(f"timed out reading sensor {sensor_index}")

    def read_all_distances_mm(self) -> list[ToFReading]:
        return [self.read_distance_mm(index) for index in range(len(self.channels))]
