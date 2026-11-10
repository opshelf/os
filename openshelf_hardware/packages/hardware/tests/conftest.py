from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

import hardware.motor


@pytest.fixture(autouse=True)
def motion_fault_dumps_stay_out_of_the_runtime_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The dump directory is relative to the working directory, so a run started from
    # openshelf_hardware/ wrote test dumps next to a device's real ones, where the
    # newest-256 prune could evict them.
    monkeypatch.setattr(hardware.motor, "MOTION_FAULT_DUMP_DIR", tmp_path / "motion_faults")


@pytest.fixture
def mock_controller() -> AsyncMock:
    controller = AsyncMock()
    controller.write_single_register = AsyncMock()
    controller.read_holding_registers = AsyncMock(return_value=(1 << 5) | (1 << 6))
    controller.read_holding_register_block = AsyncMock(return_value=((1 << 5) | (1 << 6), 0))
    controller.client = Mock()
    controller.client.connected = True
    return controller
