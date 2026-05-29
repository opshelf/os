from __future__ import annotations

import statistics
import time
from pathlib import Path

import cv2

from hardware.tof import ToFReading, ToFSensorBus
from hardware.uvc_camera import UvcCamera
from openshelf_os.config.models import IOBoxSizerConfig
from openshelf_os.iobox_sizer.models import (
    IOBoxSizerDimensions,
    IOBoxSizerMeasurement,
    IOBoxSizerToFReading,
)
from openshelf_os.iobox_sizer.vision import detect_object


class IOBoxSizerService:
    def __init__(
        self,
        *,
        config: IOBoxSizerConfig,
        camera: UvcCamera,
        tof_bus: ToFSensorBus,
    ) -> None:
        self.config = config
        self.camera = camera
        self.tof_bus = tof_bus

    def start(self) -> None:
        self.camera.start()
        self.tof_bus.connect()

    def close(self) -> None:
        self.camera.stop()
        self.tof_bus.close()

    def capture_frame(self):
        return self.camera.capture_raw()

    def capture_camera_blank(self) -> str:
        frame = self.camera.capture_raw()
        path = Path(self.config.detection.blank_frame_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(path), frame)
        return str(path)

    def calibrate_tof_blank(self, samples: int = 5) -> list[float | None]:
        sample_count = max(1, samples)
        accumulators: list[list[float]] = [[] for _ in self.config.tof.channels]

        for _ in range(sample_count):
            for reading in self.tof_bus.read_all_distances_mm():
                accumulators[reading.sensor_index].append(reading.distance_mm)

        return [(sum(values) / len(values)) if values else None for values in accumulators]

    def measure(self) -> IOBoxSizerMeasurement:
        started = time.monotonic()
        frame = self.camera.capture_raw()
        frame_height, frame_width = frame.shape[:2]
        detection = detect_object(frame, self.config.detection)

        tof_readings = self._read_tof()
        selected = self._select_reading(tof_readings)
        warnings: list[str] = []
        dimensions = None

        if detection.detected is True:
            if (
                self.config.calibration.mm_per_px_x is not None
                and self.config.calibration.mm_per_px_y is not None
            ):
                depth_scale = self._camera_depth_scale(selected)
                if self.config.calibration.apply_depth_compensation is True and depth_scale is None:
                    warnings.append("camera_depth_compensation_unavailable")

                dimensions = IOBoxSizerDimensions(
                    width=detection.width_px
                    * self.config.calibration.mm_per_px_x
                    * (depth_scale or 1.0),
                    height=detection.height_px
                    * self.config.calibration.mm_per_px_y
                    * (depth_scale or 1.0),
                    length=selected.depth_delta_mm if selected is not None else None,
                )
            else:
                warnings.append("camera_metric_calibration_missing")

        debug_path = None
        if self.config.history.save_debug_images is True:
            debug_path = self._save_debug_frame(frame)

        return IOBoxSizerMeasurement(
            detected=detection.detected,
            mode=self.config.detection.mode,
            width_px=detection.width_px,
            height_px=detection.height_px,
            center_x=detection.center_x,
            center_y=detection.center_y,
            frame_width=frame_width,
            frame_height=frame_height,
            depth_mm=selected.depth_mm if selected is not None else None,
            depth_delta_mm=selected.depth_delta_mm if selected is not None else None,
            sensor_index=selected.sensor_index if selected is not None else None,
            sensor_channel=selected.sensor_channel if selected is not None else None,
            tof_readings=tof_readings,
            dimensions_mm=dimensions,
            calibration_warnings=warnings,
            duration_ms=int((time.monotonic() - started) * 1000),
            debug_image_path=debug_path,
        )

    def _read_tof(self) -> list[IOBoxSizerToFReading]:
        sample_count = self.config.tof.samples
        accumulators: list[list[float]] = [[] for _ in self.config.tof.channels]
        errors: list[str | None] = [None for _ in self.config.tof.channels]

        for sample_index in range(sample_count):
            for index in range(len(self.config.tof.channels)):
                try:
                    raw = self.tof_bus.read_distance_mm(index)
                except Exception as exc:
                    if errors[index] is None:
                        errors[index] = str(exc)
                    continue

                accumulators[index].append(raw.distance_mm)

            if sample_index < sample_count - 1 and self.config.tof.sample_interval_ms > 0:
                time.sleep(self.config.tof.sample_interval_ms / 1000)

        readings: list[IOBoxSizerToFReading] = []
        for index, channel in enumerate(self.config.tof.channels):
            values = accumulators[index]
            if values:
                raw = ToFReading(
                    sensor_index=index,
                    channel=channel,
                    distance_mm=self._aggregate_tof_samples(values),
                )
                readings.append(self._build_tof_reading(raw))
                continue

            readings.append(
                IOBoxSizerToFReading(
                    sensor_index=index,
                    sensor_channel=channel,
                    error=errors[index] or "sensor returned no samples",
                )
            )

        return readings

    def _aggregate_tof_samples(self, values: list[float]) -> float:
        if self.config.tof.aggregation == "mean":
            return float(statistics.mean(values))

        if self.config.tof.aggregation == "nearest":
            return min(values)

        if self.config.tof.aggregation == "farthest":
            return max(values)

        return float(statistics.median(values))

    def _camera_depth_scale(self, selected: IOBoxSizerToFReading | None) -> float | None:
        if self.config.calibration.apply_depth_compensation is False or selected is None:
            return None

        if selected.depth_mm is None or selected.sensor_index is None:
            return None

        if selected.sensor_index >= len(self.config.calibration.tof_blank_mm):
            return None

        blank = self.config.calibration.tof_blank_mm[selected.sensor_index]
        if blank is None or blank <= 0 or selected.depth_mm <= 0:
            return None

        return selected.depth_mm / blank

    def _build_tof_reading(self, raw: ToFReading) -> IOBoxSizerToFReading:
        blank = None
        if raw.sensor_index < len(self.config.calibration.tof_blank_mm):
            blank = self.config.calibration.tof_blank_mm[raw.sensor_index]

        return IOBoxSizerToFReading(
            sensor_index=raw.sensor_index,
            sensor_channel=raw.channel,
            depth_mm=raw.distance_mm,
            depth_delta_mm=(blank - raw.distance_mm) if blank is not None else None,
        )

    def _select_reading(self, readings: list[IOBoxSizerToFReading]) -> IOBoxSizerToFReading | None:
        with_delta = [
            reading
            for reading in readings
            if reading.depth_delta_mm is not None and reading.error is None
        ]
        if with_delta:
            return max(with_delta, key=lambda reading: abs(reading.depth_delta_mm or 0))

        for reading in readings:
            if reading.depth_mm is not None and reading.error is None:
                return reading

        return None

    def _save_debug_frame(self, frame) -> str:
        directory = Path(self.config.history.debug_image_dir)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"capture_{int(time.time() * 1000)}.jpg"
        cv2.imwrite(str(path), frame)
        return str(path)
