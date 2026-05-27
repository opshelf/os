# ruff: noqa: E402

import pathlib
import sys

BASE = pathlib.Path(__file__).resolve()
sys.path.insert(0, str(BASE.parents[1] / "src"))

from openshelf_os import device


def test_get_device_id_prefers_environment(monkeypatch) -> None:
	monkeypatch.setenv("DEVICE_ID", "E9184E3A")
	monkeypatch.setattr(device, "DEVICE_ID_PATH", pathlib.Path("/missing-device-id"))
	monkeypatch.setattr(device.socket, "gethostname", lambda: "fallback-host")

	assert device.get_device_id() == "e9184e3a"


def test_get_device_id_reads_device_id_file(tmp_path: pathlib.Path, monkeypatch) -> None:
	monkeypatch.delenv("DEVICE_ID", raising=False)

	device_id_path = tmp_path / "device-id"
	device_id_path.write_text("c4b74bf5\n", encoding="utf-8")

	monkeypatch.setattr(device, "DEVICE_ID_PATH", device_id_path)
	monkeypatch.setattr(device.socket, "gethostname", lambda: "fallback-host")

	assert device.get_device_id() == "c4b74bf5"


def test_get_device_id_falls_back_to_hostname(monkeypatch) -> None:
	monkeypatch.delenv("DEVICE_ID", raising=False)
	monkeypatch.setattr(device, "DEVICE_ID_PATH", pathlib.Path("/missing-device-id"))
	monkeypatch.setattr(device.socket, "gethostname", lambda: "LB2-3")

	assert device.get_device_id() == "lb2-3"
