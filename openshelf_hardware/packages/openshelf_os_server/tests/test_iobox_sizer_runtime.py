from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from openshelf_os.iobox_sizer.models import IOBoxSizerMeasurement
from openshelf_os.operating_mode import OperatingMode
from openshelf_os.runtime_state import RuntimeStatePaths
from openshelf_os_server.core import iobox_sizer
from openshelf_os_server.routers import iobox_sizer as iobox_sizer_router


class FakeService:
    def __init__(self) -> None:
        self.did_measure = False
        self.did_close = False
        self.started = threading.Event()
        self.finished = threading.Event()

    def measure(self) -> IOBoxSizerMeasurement:
        self.did_measure = True
        self.started.set()
        self.finished.wait()
        return IOBoxSizerMeasurement(
            detected=True,
            mode="brightness",
            width_px=20,
            height_px=10,
            center_x=50,
            center_y=60,
            frame_width=1280,
            frame_height=720,
            depth_mm=140.0,
            depth_delta_mm=12.0,
            sensor_index=0,
            sensor_channel=6,
            duration_ms=25,
            timestamp=datetime.now(timezone.utc),
        )

    def close(self) -> None:
        self.did_close = True


class ImmediateService:
    def __init__(self) -> None:
        self.measure_calls = 0

    def measure(self) -> IOBoxSizerMeasurement:
        self.measure_calls += 1
        return IOBoxSizerMeasurement(
            detected=True,
            mode="brightness",
            width_px=20,
            height_px=10,
            center_x=50,
            center_y=60,
            frame_width=1280,
            frame_height=720,
            depth_mm=140.0,
            depth_delta_mm=12.0,
            sensor_index=0,
            sensor_channel=6,
            duration_ms=25,
            timestamp=datetime.now(timezone.utc),
        )



class ReloadService:
    def __init__(self, name: str, events: list[str]) -> None:
        self.name = name
        self.events = events
        self.closed = False
        self.camera = object()

    def start(self) -> None:
        self.events.append(f"start:{self.name}")

    def close(self) -> None:
        self.closed = True
        self.events.append(f"close:{self.name}")


def _runtime(service: object) -> iobox_sizer.IOBoxSizerRuntime:
    return iobox_sizer.IOBoxSizerRuntime(
        config=SimpleNamespace(),
        service=service,
        available=True,
    )


@pytest.mark.asyncio
async def test_measure_iobox_size_uses_runtime_after_lock_acquired(monkeypatch: pytest.MonkeyPatch) -> None:
    first_service = ImmediateService()
    second_service = ImmediateService()

    monkeypatch.setattr(iobox_sizer, "_measure_lock", asyncio.Lock())
    monkeypatch.setattr(iobox_sizer, "_runtime", _runtime(first_service))

    async with iobox_sizer._measure_lock:
        measure_task = asyncio.create_task(iobox_sizer.measure_iobox_size())
        await asyncio.sleep(0)
        monkeypatch.setattr(iobox_sizer, "_runtime", _runtime(second_service))

    await measure_task

    assert first_service.measure_calls == 0
    assert second_service.measure_calls == 1


@pytest.mark.asyncio
async def test_stop_iobox_sizer_waits_for_cancelled_measurement_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeService()

    monkeypatch.setattr(iobox_sizer, "_measure_lock", asyncio.Lock())
    monkeypatch.setattr(iobox_sizer, "_runtime", _runtime(service))
    monkeypatch.setattr(iobox_sizer, "unregister_camera_manager", AsyncMock())

    measurement_task = asyncio.create_task(iobox_sizer.measure_iobox_size())
    await asyncio.to_thread(service.started.wait)

    measurement_task.cancel()
    await asyncio.sleep(0)

    stop_task = asyncio.create_task(iobox_sizer.stop_iobox_sizer())
    await asyncio.sleep(0)

    assert stop_task.done() is False
    assert service.did_close is False

    service.finished.set()

    with pytest.raises(asyncio.CancelledError):
        await measurement_task

    await stop_task

    assert service.did_close is True
    assert iobox_sizer._runtime is None


@pytest.mark.asyncio
async def test_reload_iobox_sizer_config_serializes_full_stop_and_init(monkeypatch: pytest.MonkeyPatch) -> None:
    events = ["start:initial"]
    created_services: list[ReloadService] = []
    config = SimpleNamespace(
        enabled=True,
        tof=SimpleNamespace(channels=[6]),
        camera=SimpleNamespace(),
        detection=SimpleNamespace(blank_frame_path="data/iobox_sizer/blank_frame.png"),
    )

    def did_make_service(current_config: object) -> ReloadService:
        assert current_config is not config
        service = ReloadService(f"reload-{len(created_services) + 1}", events)
        created_services.append(service)
        return service

    monkeypatch.setattr(iobox_sizer, "_measure_lock", asyncio.Lock())
    monkeypatch.setattr(iobox_sizer, "_reload_lock", asyncio.Lock())
    monkeypatch.setattr(iobox_sizer, "_runtime", _runtime(ReloadService("initial", events)))
    monkeypatch.setattr(
        iobox_sizer,
        "get_root_config",
        lambda: SimpleNamespace(robot=SimpleNamespace(io=SimpleNamespace(sizer=config))),
    )
    monkeypatch.setattr(iobox_sizer, "_make_service", did_make_service)
    monkeypatch.setattr(iobox_sizer, "init_sizer_camera_manager", AsyncMock())
    monkeypatch.setattr(iobox_sizer, "unregister_camera_manager", AsyncMock())

    await asyncio.gather(
        iobox_sizer.reload_iobox_sizer_config(),
        iobox_sizer.reload_iobox_sizer_config(),
    )

    assert events == [
        "start:initial",
        "close:initial",
        "start:reload-1",
        "close:reload-1",
        "start:reload-2",
    ]
    assert created_services[0].closed is True
    assert created_services[1].closed is False
    assert iobox_sizer._runtime is not None
    assert iobox_sizer._runtime.service is created_services[1]


@pytest.mark.asyncio
async def test_init_iobox_sizer_keeps_shared_config_blank_path_logical_across_modes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    events: list[str] = []
    config = SimpleNamespace(
        enabled=True,
        tof=SimpleNamespace(channels=[6]),
        camera=SimpleNamespace(),
        detection=SimpleNamespace(blank_frame_path="data/iobox_sizer/blank_frame.png"),
    )
    root_config = SimpleNamespace(robot=SimpleNamespace(io=SimpleNamespace(sizer=config)))
    shared_config_path = tmp_path / "shared-config.json"

    def make_paths(mode: OperatingMode) -> RuntimeStatePaths:
        return RuntimeStatePaths(
            mode=mode,
            data_dir=tmp_path,
            config_path=shared_config_path,
            database_path=tmp_path / "base.db",
            calibration_dir=tmp_path / "calibration" / mode.value,
            workspace_manifest_path=tmp_path / f".workspace.{mode.value}.json",
        )

    def did_make_service(current_config: object) -> ReloadService:
        service = ReloadService(str(len(events)), events)
        return service

    monkeypatch.setattr(iobox_sizer, "get_root_config", lambda: root_config)
    monkeypatch.setattr(iobox_sizer, "_make_service", did_make_service)
    monkeypatch.setattr(iobox_sizer, "init_sizer_camera_manager", AsyncMock())
    monkeypatch.setattr(iobox_sizer, "unregister_camera_manager", AsyncMock())

    try:
        monkeypatch.setattr(
            RuntimeStatePaths,
            "current",
            classmethod(lambda cls: make_paths(OperatingMode.development)),
        )
        await iobox_sizer.init_iobox_sizer()
        assert iobox_sizer._runtime is not None
        assert iobox_sizer._runtime.config.detection.blank_frame_path.endswith("blank_frame.development.png")
        assert config.detection.blank_frame_path == "data/iobox_sizer/blank_frame.png"

        await iobox_sizer.stop_iobox_sizer()
        monkeypatch.setattr(
            RuntimeStatePaths,
            "current",
            classmethod(lambda cls: make_paths(OperatingMode.demo)),
        )
        await iobox_sizer.init_iobox_sizer()
        assert iobox_sizer._runtime is not None
        assert iobox_sizer._runtime.config.detection.blank_frame_path.endswith("blank_frame.demo.png")
        assert config.detection.blank_frame_path == "data/iobox_sizer/blank_frame.png"
    finally:
        await iobox_sizer.stop_iobox_sizer()


@pytest.mark.asyncio
async def test_capture_camera_blank_returns_runtime_path_without_saving_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logical_path = "data/iobox_sizer/blank_frame.png"
    physical_path = "/runtime/data/iobox_sizer/blank_frame.development.png"
    root_config = SimpleNamespace(
        robot=SimpleNamespace(
            io=SimpleNamespace(sizer=SimpleNamespace(detection=SimpleNamespace(blank_frame_path=logical_path)))
        )
    )
    service = SimpleNamespace(capture_camera_blank=lambda: physical_path)
    save_root_config = MagicMock()

    monkeypatch.setattr(iobox_sizer_router, "get_iobox_sizer_or_error", lambda: service)
    monkeypatch.setattr(iobox_sizer_router, "get_root_config", lambda: root_config)
    monkeypatch.setattr(iobox_sizer_router, "save_root_config", save_root_config)

    response = await iobox_sizer_router.capture_camera_blank_endpoint()

    assert response == {"success": True, "blank_frame_path": physical_path}
    assert root_config.robot.io.sizer.detection.blank_frame_path == logical_path
    save_root_config.assert_not_called()


@pytest.mark.asyncio
async def test_capture_store_observer_measurement_times_out_without_cancelling_measurement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finished = asyncio.Event()
    measurement = IOBoxSizerMeasurement(
        detected=True,
        mode="brightness",
        width_px=20,
        height_px=10,
        center_x=50,
        center_y=60,
        frame_width=1280,
        frame_height=720,
        depth_mm=140.0,
        depth_delta_mm=12.0,
        sensor_index=0,
        sensor_channel=6,
        duration_ms=25,
        timestamp=datetime.now(timezone.utc),
    )
    config = SimpleNamespace(
        enabled=True,
        observer_mode_enabled=True,
        measure_on_store=True,
        store_measurement_timeout_s=0.01,
    )

    async def did_measure_iobox_size() -> IOBoxSizerMeasurement:
        await asyncio.sleep(0.05)
        finished.set()
        return measurement

    monkeypatch.setattr(
        iobox_sizer,
        "get_root_config",
        lambda: SimpleNamespace(robot=SimpleNamespace(io=SimpleNamespace(sizer=config))),
    )
    monkeypatch.setattr(iobox_sizer, "measure_iobox_size", did_measure_iobox_size)

    started = time.monotonic()
    await iobox_sizer.capture_store_observer_measurement(
        upca="123",
        item_name="item",
        user_id=1,
        request_correlation_id="req-1",
        command_payload={"command": "store"},
        human_dimensions=None,
        human_dimensions_source=None,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 0.05

    await asyncio.wait_for(finished.wait(), timeout=0.2)
