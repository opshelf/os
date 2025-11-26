import asyncio
import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import cv2
import numpy as np
import pytest
from unittest.mock import AsyncMock, Mock

from hardware.exceptions import CameraConnectionError, TOFError


class AttrDict(SimpleNamespace):
    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)


class _FakeTime:
    def __init__(self) -> None:
        self._now = 0.0

    def time(self) -> float:
        self._now += 0.05
        return self._now

    def sleep(self, seconds: float) -> None:
        self._now += seconds


class _FakeVideoCapture:
    def __init__(self, index: int, *_args: Any, **_kwargs: Any) -> None:
        self.index = index
        self._opened = True
        self.settings: dict[int, Any] = {}
        self._frame = np.zeros((480, 640, 3), dtype=np.uint8)

    def isOpened(self) -> bool:
        return True

    def set(self, key: int, value: Any) -> bool:
        self.settings[key] = value
        return True

    def read(self) -> tuple[bool, np.ndarray]:
        return True, self._frame.copy()

    def grab(self) -> bool:
        return True

    def release(self) -> None:
        self._opened = False


def _install_fake_arducam() -> ModuleType:
    if "ArducamDepthCamera" in sys.modules:
        return sys.modules["ArducamDepthCamera"]

    module = ModuleType("ArducamDepthCamera")

    class FrameType:
        DEPTH = 0

    class Connection:
        CSI = 0

    class Control:
        RANGE = 0

    class DepthData:
        def __init__(self) -> None:
            self.depth_data = np.ones((2, 2), dtype=np.uint16)
            self.confidence_data = np.ones((2, 2), dtype=np.uint8)

    class ArducamCamera:
        def __init__(self) -> None:
            self.started = False

        def open(self, *_args: Any, **_kwargs: Any) -> int:
            return 0

        def start(self, *_args: Any, **_kwargs: Any) -> int:
            self.started = True
            return 0

        def setControl(self, *_args: Any, **_kwargs: Any) -> int:
            return 0

        def requestFrame(self, *_args: Any, **_kwargs: Any) -> DepthData:
            return DepthData()

        def releaseFrame(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def stop(self) -> None:
            return None

        def close(self) -> None:
            return None

    module.FrameType = FrameType
    module.Connection = Connection
    module.Control = Control
    module.DepthData = DepthData
    module.ArducamCamera = ArducamCamera
    sys.modules["ArducamDepthCamera"] = module
    return module


@pytest.fixture(autouse=True)
def patch_iocam(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_ac = _install_fake_arducam()
    monkeypatch.setattr("hardware.iocam.ac", fake_ac, raising=False)
    monkeypatch.setattr("hardware.iocam._find_innomaker_device", lambda: 0, raising=False)
    monkeypatch.setattr("cv2.VideoCapture", _FakeVideoCapture)

    async def _fake_tof_loop(self) -> None:
        while not self._stop_evt.is_set():
            await asyncio.sleep(0.001)

    def _fake_init_tof(self) -> None:
        cam = fake_ac.ArducamCamera()
        self._tof = cam
        start_ret = cam.start(fake_ac.FrameType.DEPTH) if hasattr(cam, "start") else 0
        if start_ret != 0:
            raise TOFError("Failed to start mock TOF camera")
        depth = fake_ac.DepthData()
        self._update_depth_buffers(depth.depth_data, depth.confidence_data)

    def _fake_init_usb(self) -> None:
        self._usb = cv2.VideoCapture(self._usb_index)
        if not self._usb.isOpened():
            raise CameraConnectionError("Failed to open mock USB camera")
        if self._usb_fourcc:
            fourcc = cv2.VideoWriter_fourcc(*self._usb_fourcc)
            self._usb.set(cv2.CAP_PROP_FOURCC, fourcc)
        if self._usb_exposure is not None:
            self._usb.set(cv2.CAP_PROP_EXPOSURE, float(self._usb_exposure))

    monkeypatch.setattr("hardware.iocam.AFSCam._tof_loop", _fake_tof_loop)
    monkeypatch.setattr("hardware.iocam.AFSCam._init_tof", _fake_init_tof)
    monkeypatch.setattr("hardware.iocam.AFSCam._init_usb", _fake_init_usb)


@pytest.fixture(autouse=True)
def patch_toolcam_time(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_time = _FakeTime()
    monkeypatch.setattr("hardware.toolcam.time.time", fake_time.time)
    monkeypatch.setattr("hardware.toolcam.time.sleep", fake_time.sleep)


@pytest.fixture
def mock_modbus_client(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    client = AsyncMock()
    client.connected = True

    async def _connect() -> bool:
        client.connected = True
        return True

    client.connect = AsyncMock(side_effect=_connect)
    client.read_holding_registers = AsyncMock()
    client.write_register = AsyncMock()
    client.write_registers = AsyncMock()
    monkeypatch.setattr(
        "hardware.controller.AsyncModbusSerialClient",
        lambda *_args, **_kwargs: client,
    )
    return client


@pytest.fixture
def mock_controller() -> AsyncMock:
    controller = AsyncMock()
    controller.write_single_register = AsyncMock()
    controller.write_registers = AsyncMock()
    controller.read_holding_registers = AsyncMock(return_value=(1 << 5) | (1 << 6))
    controller.client = Mock()
    controller.client.connected = True
    return controller


@pytest.fixture
def sample_tof_params() -> AttrDict:
    return AttrDict(csi_port=0, max_distance_mm=2000, confidence_min=0.5)


@pytest.fixture
def sample_usb_params() -> AttrDict:
    return AttrDict(
        usb_index=0,
        usb_fourcc="MJPG",
        usb_exposure=10,
        square_mm=25.0,
        fx_px=600.0,
        fy_px=600.0,
        px_per_mm=2.0,
    )


@pytest.fixture
def sample_camera_params() -> AttrDict:
    mtx = np.eye(3, dtype=np.float32)
    dist = np.zeros((1, 5), dtype=np.float32)
    newcameramtx = mtx.copy()
    rvecs = np.zeros((1, 3), dtype=np.float32)
    tvecs = np.zeros((1, 3), dtype=np.float32)
    map1 = np.zeros((2, 2), dtype=np.float32)
    map2 = np.zeros((2, 2), dtype=np.float32)
    params = AttrDict(
        url=0,
        mtx=mtx,
        dist=dist,
        rvecs=rvecs,
        tvecs=tvecs,
        newcameramtx=newcameramtx,
        roi=(0, 0, 1, 1),
        H=np.eye(3, dtype=np.float32),
        map1=map1,
        map2=map2,
        map_size=(2, 2),
        H_ref_size=(640, 480),
        board_W_mm=100.0,
        board_H_mm=100.0,
        pattern_size=(7, 7),
        square_size_mm=25.0,
    )
    return params
