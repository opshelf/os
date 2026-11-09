"""The config editor endpoints write the robot config file: they must never expose or write `auth`,
must write nothing while the robot is busy, and must report invalid fields by path."""

import json

import pytest

from openshelf_os_server import settings
from openshelf_os_server.routers import system


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(settings._build_minimal_config({}), indent="\t"))
    monkeypatch.setattr(settings, "CONFIG_PATH", path)
    settings.clear_config_cache()
    restarts = []
    monkeypatch.setattr(system, "_schedule_api_restart", lambda: restarts.append(True))
    yield path, restarts
    settings.clear_config_cache()


@pytest.mark.anyio
async def test_auth_section_is_neither_readable_nor_writable(client, config_path):
    path, _ = config_path
    before = path.read_bytes()

    read = await client.get("/api/system/config", params={"section": "auth"})
    write = await client.put(
        "/api/system/config",
        json={"section": "auth", "data": {"jwt_secret": "x"}, "revision": "any", "restart": False},
    )

    assert read.status_code == 404
    assert write.status_code == 404
    assert path.read_bytes() == before


@pytest.mark.anyio
async def test_save_while_robot_moves_writes_nothing_and_does_not_restart(client, config_path, monkeypatch):
    path, restarts = config_path
    before = path.read_bytes()
    current = (await client.get("/api/system/config")).json()
    monkeypatch.setattr(system, "get_manual_motion_operation", lambda: "jog")

    current["data"]["tool"]["parameters"]["STAGE_NUDGE"] = 3
    response = await client.put(
        "/api/system/config",
        json={"section": "robot", "data": current["data"], "revision": current["revision"], "restart": True},
    )

    assert response.status_code == 409
    assert path.read_bytes() == before
    assert restarts == []


@pytest.mark.anyio
async def test_edit_round_trip_marks_bad_field_then_saves_only_the_section(client, config_path):
    path, restarts = config_path
    current = (await client.get("/api/system/config")).json()
    robot = current["data"]

    robot["tool"]["parameters"]["Z_MOVE_RPM"] = 0
    check = (await client.post("/api/system/config/validate", json={"section": "robot", "data": robot})).json()
    assert check["valid"] is False
    assert [issue["path"] for issue in check["issues"]] == ["robot.tool.parameters.Z_MOVE_RPM"]

    robot["tool"]["parameters"]["Z_MOVE_RPM"] = 40
    saved = await client.put(
        "/api/system/config",
        json={"section": "robot", "data": robot, "revision": current["revision"], "restart": True},
    )

    assert saved.status_code == 200, saved.text
    assert restarts == [True]
    on_disk = json.loads(path.read_text())
    assert on_disk["robot"]["tool"]["parameters"]["Z_MOVE_RPM"] == 40
    assert "auth" not in on_disk

    stale = await client.put(
        "/api/system/config",
        json={"section": "robot", "data": robot, "revision": current["revision"], "restart": False},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "config_changed"
