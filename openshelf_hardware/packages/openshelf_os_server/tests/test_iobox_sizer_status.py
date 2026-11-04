from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import openshelf_os_server.core.iobox_sizer as sizer
from openshelf_os_server.errors import CODE_MESSAGES

RAW = "FTDI open failed: ftdi://0x0403/1 busy"


def _config():
	return SimpleNamespace(
		enabled=True,
		observer_mode_enabled=False,
		measure_on_store=False,
		camera=SimpleNamespace(model_dump=lambda mode="json": {}),
		tof=SimpleNamespace(channels=[0]),
		detection=SimpleNamespace(blank_frame_path="blank.png"),
		model_copy=lambda deep=True: _config(),
	)


@pytest.fixture(autouse=True)
def _patched(monkeypatch):
	config = _config()
	monkeypatch.setattr(sizer, "get_root_config", lambda: SimpleNamespace(robot=SimpleNamespace(io=SimpleNamespace(sizer=config))))
	monkeypatch.setattr(sizer, "init_sizer_camera_manager", MagicMock(side_effect=lambda *_: _noop()))
	monkeypatch.setattr(sizer.RuntimeStatePaths, "current", lambda: SimpleNamespace(resolve_mode_owned_path=lambda path, relative_to: path))
	yield
	monkeypatch.setattr(sizer, "_runtime", None)


async def _noop() -> None:
	return None


def _assert_status_clean(message: str) -> None:
	status = sizer.get_iobox_sizer_status()
	assert "last_error_type" not in status
	assert status["last_error"] == message
	assert RAW not in status["last_error"]


async def test_camera_start_failure_status_is_operator_sentence(monkeypatch) -> None:
	service = MagicMock()
	service.camera.start.side_effect = RuntimeError(RAW)
	monkeypatch.setattr(sizer, "_make_service", lambda _config: service)

	await sizer.init_iobox_sizer()

	_assert_status_clean(CODE_MESSAGES["iobox_sizer_unavailable"])


async def test_tof_failure_status_is_operator_sentence(monkeypatch) -> None:
	service = MagicMock()
	service.tof_bus.connect.side_effect = RuntimeError(RAW)
	monkeypatch.setattr(sizer, "_make_service", lambda _config: service)

	await sizer.init_iobox_sizer()

	_assert_status_clean(sizer.TOF_UNAVAILABLE_MESSAGE)


async def test_measurement_failure_status_is_operator_sentence(monkeypatch) -> None:
	service = MagicMock()
	service.measure.side_effect = RuntimeError(RAW)
	monkeypatch.setattr(sizer, "_make_service", lambda _config: service)
	await sizer.init_iobox_sizer()
	assert sizer.get_iobox_sizer_status()["last_error"] is None

	with pytest.raises(RuntimeError):
		await sizer.measure_iobox_size()

	_assert_status_clean(sizer.MEASUREMENT_FAILED_MESSAGE)
