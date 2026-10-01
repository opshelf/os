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


def test_get_device_alias_reads_alias_file(tmp_path: pathlib.Path, monkeypatch) -> None:
	alias_path = tmp_path / "device-alias"
	alias_path.write_text("lb4a-3\n", encoding="utf-8")

	monkeypatch.setattr(device, "DEVICE_ALIAS_PATH", alias_path)

	assert device.get_device_alias() == "lb4a-3"


def test_get_device_alias_is_empty_without_file(monkeypatch) -> None:
	monkeypatch.setattr(device, "DEVICE_ALIAS_PATH", pathlib.Path("/missing-device-alias"))

	assert device.get_device_alias() == ""


def test_get_release_reads_release_file(tmp_path: pathlib.Path, monkeypatch) -> None:
	monkeypatch.delenv("RELEASE_SHA", raising=False)
	monkeypatch.delenv("RELEASE_BRANCH", raising=False)

	release_path = tmp_path / "release"
	release_path.write_text("7F3A2B1C9D4E5F6A7B8C9D0E1F2A3B4C5D6E7F8A\nmain\n", encoding="utf-8")

	monkeypatch.setattr(device, "RELEASE_PATH", release_path)

	assert device.get_release_sha() == "7f3a2b1c9d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a"
	assert device.get_release_branch() == "main"


def test_get_release_prefers_environment(tmp_path: pathlib.Path, monkeypatch) -> None:
	release_path = tmp_path / "release"
	release_path.write_text("stale-sha\nstale-branch\n", encoding="utf-8")

	monkeypatch.setattr(device, "RELEASE_PATH", release_path)
	monkeypatch.setenv("RELEASE_SHA", "E9184E3A")
	monkeypatch.setenv("RELEASE_BRANCH", "Feat/Status")

	assert device.get_release_sha() == "e9184e3a"
	assert device.get_release_branch() == "feat/status"


def test_get_release_is_empty_without_file(monkeypatch) -> None:
	monkeypatch.delenv("RELEASE_SHA", raising=False)
	monkeypatch.delenv("RELEASE_BRANCH", raising=False)
	monkeypatch.setattr(device, "RELEASE_PATH", pathlib.Path("/missing-release"))

	assert device.get_release_sha() == ""
	assert device.get_release_branch() == ""


def test_get_release_branch_is_empty_with_sha_only_file(tmp_path: pathlib.Path, monkeypatch) -> None:
	release_path = tmp_path / "release"
	release_path.write_text("7f3a2b1c\n", encoding="utf-8")

	monkeypatch.delenv("RELEASE_SHA", raising=False)
	monkeypatch.delenv("RELEASE_BRANCH", raising=False)
	monkeypatch.setattr(device, "RELEASE_PATH", release_path)

	assert device.get_release_sha() == "7f3a2b1c"
	assert device.get_release_branch() == ""
