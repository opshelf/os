from types import SimpleNamespace
from typing import Any

import cv2
import numpy as np
import pytest
from unittest.mock import AsyncMock, Mock

from hardware.exceptions import CameraConnectionError


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


@pytest.fixture(autouse=True)
def patch_iocam(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hardware.iocam._find_innomaker_device", lambda: 0, raising=False)
    monkeypatch.setattr("cv2.VideoCapture", _FakeVideoCapture)

    def _fake_init_usb(self, *args: Any, use_auto_exposure: bool = True, **kwargs: Any) -> None:
        self._usb = cv2.VideoCapture(self._usb_index)
        if not self._usb.isOpened():
            raise CameraConnectionError("Failed to open mock USB camera")
        if self._usb_fourcc:
            fourcc = cv2.VideoWriter_fourcc(*self._usb_fourcc)
            self._usb.set(cv2.CAP_PROP_FOURCC, fourcc)
        if use_auto_exposure is True:
            self._usb.set(cv2.CAP_PROP_AUTO_EXPOSURE, 3)
        elif self._usb_exposure is not None:
            self._usb.set(cv2.CAP_PROP_EXPOSURE, float(self._usb_exposure))

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
    client.read_input_registers = AsyncMock()
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
def sample_usb_params() -> AttrDict:
    return AttrDict(
        usb_index=0,
        usb_fourcc="MJPG",
        usb_exposure=10,
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
