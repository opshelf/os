from __future__ import annotations

import pytest
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


@pytest.mark.parametrize(
    "aggregation, depths, deltas",
    [
        pytest.param("median", (120.0, 305.0), (80.0, 95.0), id="median"),
        pytest.param("mean", (126.66666666666667, 305.0), None, id="mean"),
        pytest.param("nearest", (100.0, 300.0), None, id="nearest"),
        pytest.param("farthest", (160.0, 310.0), None, id="farthest"),
    ],
)
def test_iobox_sizer_service_aggregates_tof_samples(
    aggregation: str,
    depths: tuple[float, float],
    deltas: tuple[float, float] | None,
) -> None:
    service = IOBoxSizerService(
        config=IOBoxSizerConfig(
            tof={"channels": [6, 7], "samples": 3, "aggregation": aggregation},
            calibration={"tof_blank_mm": [200.0, 400.0]},
        ),
        camera=FakeCamera(),
        tof_bus=SequencedToFBus(),
    )

    readings = service._read_tof()

    assert readings[0].depth_mm == depths[0]
    assert readings[1].depth_mm == depths[1]
    if deltas is not None:
        assert readings[0].depth_delta_mm == deltas[0]
        assert readings[1].depth_delta_mm == deltas[1]


def test_iobox_sizer_service_can_prefer_specific_tof_sensor() -> None:
    service = IOBoxSizerService(
        config=IOBoxSizerConfig(
            tof={"channels": [6, 7], "preferred_sensor_index": 0},
            calibration={"tof_blank_mm": [200.0, 300.0]},
        ),
        camera=FakeCamera(),
        tof_bus=FakeToFBus(),
    )
    readings = service._read_tof()

    selected = service._select_reading(readings)

    assert selected is readings[0]
