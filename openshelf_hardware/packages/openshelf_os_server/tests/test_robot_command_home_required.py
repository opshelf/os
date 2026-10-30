from __future__ import annotations

import pytest
from fastapi import HTTPException

from openshelf_os.robot.commands import RecoverCommand, RetrieveItemCommand, StopCommand, StoreItemCommand
from openshelf_os_server.routers.robot import _ensure_command_allowed_while_needs_home


class _Worker:
    def __init__(self, is_needs_home: bool, is_crashed: bool = False) -> None:
        self.is_needs_home = is_needs_home
        self.is_crashed = is_crashed

    def needs_home(self) -> bool:
        return self.is_needs_home

    def needs_reset(self) -> bool:
        return self.is_needs_home or self.is_crashed

    def needs_home_axes(self) -> list[str]:
        return ["stage"]

    def needs_home_reason(self) -> str:
        return "Startup failed for motion components: stage"


def test_robot_command_home_required_allows_stop_command() -> None:
    _ensure_command_allowed_while_needs_home(StopCommand(), _Worker(True))


def test_robot_command_home_required_blocks_normal_command() -> None:
    with pytest.raises(HTTPException) as exc_info:
        _ensure_command_allowed_while_needs_home(
            StoreItemCommand(item={"barcode": "123"}),
            _Worker(True),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == {
        "message": "Home required before robot commands.",
        "code": "home_required",
        "needs_home": True,
        "needs_home_axes": ["stage"],
        "reason": "Startup failed for motion components: stage",
    }


def test_a_grip_fault_crash_blocks_commands_until_recovered() -> None:
    """A grip fault homes nothing, so the crash itself has to hold the next command back."""
    worker = _Worker(False, is_crashed=True)
    location = {"side": "front_module", "shelf_idx": 0, "cell_idx": 0}

    with pytest.raises(HTTPException) as exc_info:
        _ensure_command_allowed_while_needs_home(RetrieveItemCommand(location=location), worker)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "reset_required"
    _ensure_command_allowed_while_needs_home(RecoverCommand(), worker)
