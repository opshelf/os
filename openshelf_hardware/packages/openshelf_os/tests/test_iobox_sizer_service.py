from __future__ import annotations

from hardware.tof import ToFReading
from openshelf_os.config.models import IOBoxSizerConfig
from openshelf_os.iobox_sizer.service import IOBoxSizerService


class FakeCamera:
    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def capture_raw(self):
        raise AssertionError("not used")


class FakeToFBus:
    def read_distance_mm(self, sensor_index: int) -> ToFReading:
        if sensor_index == 0:
            return ToFReading(sensor_index=0, channel=6, distance_mm=120.0)

        raise RuntimeError("sensor 1 failed")


def test_iobox_sizer_service_reads_tof_per_channel() -> None:
    service = IOBoxSizerService(
        config=IOBoxSizerConfig(
            tof={"channels": [6, 7]},
            calibration={"tof_blank_mm": [200.0, 300.0]},
        ),
        camera=FakeCamera(),
        tof_bus=FakeToFBus(),
    )

    readings = service._read_tof()

    assert readings[0].model_dump() == {
        "sensor_index": 0,
        "sensor_channel": 6,
        "depth_mm": 120.0,
        "depth_delta_mm": 80.0,
        "error": None,
    }
    assert readings[1].model_dump() == {
        "sensor_index": 1,
        "sensor_channel": 7,
        "depth_mm": None,
        "depth_delta_mm": None,
        "error": "sensor 1 failed",
    }


class SequencedToFBus:
    def __init__(self) -> None:
        self.values = {
            0: [100.0, 160.0, 120.0],
            1: [310.0, 300.0, 305.0],
        }

    def read_distance_mm(self, sensor_index: int) -> ToFReading:
        value = self.values[sensor_index].pop(0)
        return ToFReading(
            sensor_index=sensor_index,
            channel=6 + sensor_index,
            distance_mm=value,
        )


def test_iobox_sizer_service_can_median_filter_tof_samples() -> None:
    service = IOBoxSizerService(
        config=IOBoxSizerConfig(
            tof={"channels": [6, 7], "samples": 3},
            calibration={"tof_blank_mm": [200.0, 400.0]},
        ),
        camera=FakeCamera(),
        tof_bus=SequencedToFBus(),
    )

    readings = service._read_tof()

    assert readings[0].depth_mm == 120.0
    assert readings[0].depth_delta_mm == 80.0
    assert readings[1].depth_mm == 305.0
    assert readings[1].depth_delta_mm == 95.0


def test_iobox_sizer_service_can_mean_filter_tof_samples() -> None:
    service = IOBoxSizerService(
        config=IOBoxSizerConfig(
            tof={"channels": [6, 7], "samples": 3, "aggregation": "mean"},
            calibration={"tof_blank_mm": [200.0, 400.0]},
        ),
        camera=FakeCamera(),
        tof_bus=SequencedToFBus(),
    )

    readings = service._read_tof()

    assert readings[0].depth_mm == 126.66666666666667
    assert readings[1].depth_mm == 305.0


def test_iobox_sizer_service_can_use_nearest_tof_sample() -> None:
    service = IOBoxSizerService(
        config=IOBoxSizerConfig(
            tof={"channels": [6, 7], "samples": 3, "aggregation": "nearest"},
            calibration={"tof_blank_mm": [200.0, 400.0]},
        ),
        camera=FakeCamera(),
        tof_bus=SequencedToFBus(),
    )

    readings = service._read_tof()

    assert readings[0].depth_mm == 100.0
    assert readings[1].depth_mm == 300.0


def test_iobox_sizer_service_can_use_farthest_tof_sample() -> None:
    service = IOBoxSizerService(
        config=IOBoxSizerConfig(
            tof={"channels": [6, 7], "samples": 3, "aggregation": "farthest"},
            calibration={"tof_blank_mm": [200.0, 400.0]},
        ),
        camera=FakeCamera(),
        tof_bus=SequencedToFBus(),
    )

    readings = service._read_tof()

    assert readings[0].depth_mm == 160.0
    assert readings[1].depth_mm == 310.0
