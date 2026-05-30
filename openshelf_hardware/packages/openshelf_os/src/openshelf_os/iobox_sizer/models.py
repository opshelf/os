from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


class IOBoxSizerToFReading(BaseModel):
    sensor_index: int
    sensor_channel: int
    depth_mm: float | None = None
    depth_delta_mm: float | None = None
    error: str | None = None


class IOBoxSizerDimensions(BaseModel):
    width: float
    height: float
    length: float | None = None


class IOBoxSizerMeasurement(BaseModel):
    detected: bool
    mode: Literal["brightness", "rectangle", "difference", "circle"]
    width_px: int | None = None
    height_px: int | None = None
    center_x: float | None = None
    center_y: float | None = None
    frame_width: int
    frame_height: int
    depth_mm: float | None = None
    depth_delta_mm: float | None = None
    sensor_index: int | None = None
    sensor_channel: int | None = None
    tof_readings: list[IOBoxSizerToFReading] = Field(default_factory=list)
    dimensions_mm: IOBoxSizerDimensions | None = None
    calibration_warnings: list[str] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    duration_ms: int
    debug_image_path: str | None = None
