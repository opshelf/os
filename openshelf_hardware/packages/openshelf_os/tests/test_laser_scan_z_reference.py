from types import SimpleNamespace

import pytest

from openshelf_os.laser_calibration.core import get_scan_z_delta
from openshelf_os.storage import StorageModuleSide


def test_scan_z_delta_pins_reference_z_when_stage_extension_differs() -> None:
    scan_config = SimpleNamespace(
        right_z_reference=SimpleNamespace(
            shelf_idx=0,
            cell_idx=0,
            z_offset_mm=8.0,
            stage_extension_mm=46.0,
        ),
    )

    z_delta = get_scan_z_delta(
        scan_config=scan_config,
        side=StorageModuleSide.RIGHT,
        reference_wall_depth_mm=9.5,
        current_stage_extension_mm=44.0,
    )

    assert 9.5 + z_delta["delta_mm"] == pytest.approx(8.0)
