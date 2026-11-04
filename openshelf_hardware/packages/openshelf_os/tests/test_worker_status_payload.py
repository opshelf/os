"""The published worker status keeps the commands screens track and drops the rest."""
from __future__ import annotations

from openshelf_os.robot.commands import HomeCommand, MoveCommand, RecoverCommand
from openshelf_os.robot.worker import _transform_worker_status_payload


def transform(current: object, queued: list[object]) -> dict:
    return _transform_worker_status_payload(
        {
            "current_command": current,
            "queued_operations": [{"command": command, "command_id": "c1"} for command in queued],
            "queue_length": len(queued),
        }
    )


def test_a_running_reset_home_stays_visible_so_the_kiosk_sees_it_start_and_end() -> None:
    status = transform(HomeCommand().model_dump(mode="json"), [])

    assert status["current_command"] == {"command": "home"}


def test_queued_reset_commands_keep_their_axes() -> None:
    status = transform(
        None,
        [
            HomeCommand(axes=["x_axis"]).model_dump(mode="json"),
            RecoverCommand(axes=["stage"]).model_dump(mode="json"),
        ],
    )

    assert [operation["command"] for operation in status["queued_operations"]] == [
        {"command": "home", "axes": ["x_axis"]},
        {"command": "recover", "axes": ["stage"]},
    ]


def test_other_maintenance_commands_stay_hidden() -> None:
    move = MoveCommand.model_validate({"coordinate": {"x": 1, "y": 2}}).model_dump(mode="json")

    status = transform(move, [move])

    assert status["current_command"] is None
    assert status["queued_operations"] == []
