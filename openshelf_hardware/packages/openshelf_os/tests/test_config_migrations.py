from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from openshelf_os.config.loader import load_config_file
from openshelf_os.config.exceptions import ConfigValidationError
from openshelf_os.config.root import RootConfig
from openshelf_os.config_migrations import (
    apply,
    cli,
    runner,
    v005_module_side_names as v005,
    v006_multi_item_io_pose as v006,
)
from openshelf_os.config_migrations.v002_laser_parameter_names import (
    SCAN_KEY_RENAMES,
    REMOVED_SCAN_KEYS,
)
from openshelf_os.config_migrations.v003_remove_unused_scan_keys import (
    REMOVED_SCAN_KEYS as UNUSED_SCAN_KEYS,
)
from openshelf_os.config_migrations.v004_promote_scan_constants import (
    REMOVED_SCAN_KEYS as PROMOTED_SCAN_KEYS,
)
from openshelf_os.config_migrations.v007_remove_z_stall_probe_keys import (
    REMOVED_SCAN_KEYS as Z_STALL_PROBE_SCAN_KEYS,
)
from openshelf_os.laser_calibration.core import (
    get_gripper_xy_correction,
    get_laser_scan_pose_stage_mm,
    get_wall_depth_to_gripper_z_model,
    require_z_offset_model_matches_z_place,
    z_offset_model_intercept_at_stage,
)
from openshelf_os.laser_calibration.motion import get_laser_scan_z_extension_mm
from openshelf_os.storage import StorageModuleSide


@pytest.fixture
def legacy_config():
    return {
        "name": "migration-test",
        "version": "0.1.0",
        "robot": {
            "tool": {
                "parameters": {
                    "IDLE_POSITION": [0, 0],
                    "PICKUP_POSITION_L": [0, 0],
                    "PICKUP_POSITION_H": [0, 0],
                    "SCAN_POSITION": [930, 1],
                    "STAGE_EXTENSION_IO_BOX": 0,
                    "STAGE_NUDGE": 0,
                    "STAGE_EXTENSION_SHELF_FRONT": 46,
                    "STAGE_EXTENSION_SHELF_BACK": 47,
                    "Z_EXTENSION_PICK": 0,
                    "Z_EXTENSION_PLACE": 160.5,
                    "Z_EXTENSION_IO": 0,
                    "Z_MOVE_RPM": 20,
                    "Z_MOVE_ACCEL": 100,
                    "ROTATION_FRONT": 182.25,
                    "ROTATION_BACK": 2.5,
                },
                "motors": [],
                "poe_camera": {"url": ""},
            },
            "io": {"motors": []},
            "grid": {
                "path": "storage.development.json",
                "scan": {
                    "laser_offset_x_mm": 51,
                    "laser_offset_y_mm": 0,
                    "laser_scan_stage_extension_mm": 13,
                    "laser_scan_z_offset_from_place_mm": -40.5,
                    "left_xy_offset": {"x_mm": -0.8, "y_mm": -3.25},
                    "right_xy_offset": {"x_mm": -99.25, "y_mm": -2.88},
                    "left_z_offset_model": {
                        "intercept_mm": 1.587,
                        "slope_per_mm": -0.00285,
                        "stage_extension_mm": 46,
                        "z_place_mm": 160.5,
                    },
                    "right_z_offset_model": {
                        "intercept_mm": 1.058,
                        "slope_per_mm": 0,
                        "stage_extension_mm": 47,
                        "z_place_mm": 160.5,
                    },
                    "z_extension_mm": None,
                    "angle_calibration_beam_zero_deg": 0,
                    "angle_calibration_method": "stage_stall",
                    "angle_calibration_z_offset_min_mm": -8,
                    "angle_calibration_alpha_max_deg": 10,
                    "angle_calibration_stage_stall_fall_threshold_mm": 0.15,
                    "angle_calibration_stage_stall_stop_after_fall_count": 1,
                    "wall_depth_tolerance_mm": 2,
                    "laser_sample_settle_ms": 50,
                    "angle_calibration_stage_stall_coarse_max_deg": 5.5,
                    "z_stall_tray_engagement_mm": 0.71,
                    "z_stall_probe_x_shift_mm": 20.0,
                    "unknown_scan_setting": {"preserve": True},
                },
            },
        },
        "auth": {"jwt_secret": "test-private-value", "require_auth": False},
        "unknown_root_setting": ["preserve", 123],
    }


def test_migration_preserves_pose_geometry_and_unrelated_values(legacy_config):
    original = copy.deepcopy(legacy_config)
    migrated, changes = runner.migrate_config(legacy_config)
    assert legacy_config == original
    assert migrated["schema_version"] == 7
    assert migrated["version"] == original["version"]
    assert migrated["auth"] == original["auth"]
    assert migrated["unknown_root_setting"] == original["unknown_root_setting"]
    assert migrated["robot"]["tool"] == _with_multi_item_pose(original)["robot"]["tool"]
    assert migrated["robot"]["grid"]["scan"]["unknown_scan_setting"] == {"preserve": True}
    assert not set(REMOVED_SCAN_KEYS) & migrated["robot"]["grid"]["scan"].keys()
    assert not set(UNUSED_SCAN_KEYS) & migrated["robot"]["grid"]["scan"].keys()
    assert not set(Z_STALL_PROBE_SCAN_KEYS) & migrated["robot"]["grid"]["scan"].keys()
    assert changes[-1] == "schema_version: 6 -> 7"
    config = RootConfig.model_validate(migrated)
    scan = config.robot.grid.scan

    async def get_param(name):
        return getattr(config.robot.tool.parameters, name)

    assert get_laser_scan_pose_stage_mm(scan) == 13
    assert (
        asyncio.run(get_laser_scan_z_extension_mm(SimpleNamespace(get_param=get_param), scan))
        == 120
    )
    for side, expected_xy, expected_z in [
        (StorageModuleSide.FRONT, (50.2, -3.25), 10 + 1.587 - 0.00285 * 400),
        (StorageModuleSide.BACK, (-48.25, -2.88), 10 + 1.058),
    ]:
        dx, dy = get_gripper_xy_correction(scan_config=scan, side=side)
        assert (scan.laser_offset_x_mm + dx, -scan.laser_offset_y_mm + dy) == pytest.approx(
            expected_xy
        )
        model = get_wall_depth_to_gripper_z_model(scan_config=scan, side=side)
        assert 10 + model.intercept_mm + model.x_slope_mm_per_mm * 400 == pytest.approx(expected_z)
        # The pin survives the migration: the model is carried from the stage it was derived at.
        require_z_offset_model_matches_z_place(model=model, side=side, z_place_mm=160.5, tolerance_mm=0.2)
        assert z_offset_model_intercept_at_stage(
            model=model, stage_extension_mm=model.reference_stage_mm + 1
        ) == pytest.approx(model.intercept_mm - 1)


def test_divergent_promoted_values_are_refused_without_printing_them():
    config = {
        "schema_version": 3,
        "name": "divergent-scan",
        "version": "0.1.0",
        "robot": {
            "tool": {"parameters": {}, "motors": [], "poe_camera": {"url": ""}},
            "io": {"motors": []},
            "grid": {
                "path": "storage.json",
                "scan": {
                    # Drifted from the fleet: each of these must be refused...
                    "laser_sample_settle_ms": 120,
                    "expected_hole_width_mm": 40.0,
                    "z_stall_probe_rpm": None,
                    # ...while the fleet's null spellings must not be flagged:
                    "angle_calibration_stage_stall_target_mm": None,
                    # Null derived the probe start from the tool parameters, so it
                    # is not the stored 0.0 and is refused.
                    "angle_calibration_stage_stall_fast_approach_mm": None,
                },
            },
        },
        "auth": {"jwt_secret": "test-private-value", "require_auth": False},
    }
    with pytest.raises(ValueError) as excinfo:
        runner.migrate_config(config)
    message = str(excinfo.value)
    for key in (
        "laser_sample_settle_ms",
        "expected_hole_width_mm",
        "z_stall_probe_rpm",
        "angle_calibration_stage_stall_fast_approach_mm",
    ):
        assert f"robot.grid.scan.{key}" in message
    for key in ("angle_calibration_stage_stall_target_mm",):
        assert f"robot.grid.scan.{key}" not in message
    # Diagnostics carry paths and key names, never config values.
    assert "120" not in message
    assert "40.0" not in message
    # The step raised before dropping anything: the source document is untouched.
    assert "laser_sample_settle_ms" in config["robot"]["grid"]["scan"]


@pytest.fixture
def v4_side_config():
    return {
        "schema_version": 4,
        "name": "side-rename-test",
        "version": "0.1.0",
        "robot": {
            "tool": {
                "parameters": {
                    "IDLE_POSITION": [0, 0],
                    "PICKUP_POSITION_L": [0, 0],
                    "PICKUP_POSITION_H": [0, 0],
                    "SCAN_POSITION": [0, 0],
                    "STAGE_EXTENSION_IO_BOX": 0,
                    "STAGE_NUDGE": 0,
                    "STAGE_EXTENSION_SHELF_FRONT": 46,
                    "STAGE_EXTENSION_SHELF_BACK": 47,
                    "Z_EXTENSION_PICK": 0,
                    "Z_EXTENSION_PLACE": 160.5,
                    "Z_EXTENSION_IO": 0,
                    "Z_MOVE_RPM": 20,
                    "Z_MOVE_ACCEL": 100,
                    "ROTATION_FRONT": 182.25,
                    "ROTATION_BACK": 2.5,
                },
                "motors": [],
                "poe_camera": {"url": ""},
            },
            "io": {"motors": []},
            "grid": {
                "path": "storage.json",
                "scan": {
                    "left_shelf_scout_x_mm": 120.0,
                    "right_shelf_scout_x_mm": 45.0,
                    "left_gripper_xy_correction_mm": {"x_mm": -1.0, "y_mm": -2.0},
                    "right_gripper_xy_correction_mm": None,
                    "left_wall_depth_to_gripper_z_model": {
                        "intercept_mm": 1.5,
                        "x_slope_mm_per_mm": 0.0,
                        "reference_stage_mm": 46.0,
                        "reference_z_place_mm": 160.5,
                    },
                    "right_wall_depth_to_gripper_z_model": None,
                    "left_bounds_x_min_mm": 0.0,
                    "left_bounds_x_max_mm": 950.0,
                    "left_bounds_y_min_mm": 0.0,
                    "left_bounds_y_max_mm": 1250.0,
                    "right_bounds_x_min_mm": 0.0,
                    "right_bounds_x_max_mm": 950.0,
                    "right_bounds_y_min_mm": 0.0,
                    "right_bounds_y_max_mm": 1250.0,
                },
            },
        },
        "storage": {
            "bins": [
                {
                    "barcode": "bin-front",
                    "home_location": {"side": "left_module", "shelf_idx": 0, "cell_idx": 1},
                    "inner_dimensions": {"width": 100.0, "height": 100.0, "length": 100.0},
                    "max_items": 4,
                },
                {
                    "barcode": "bin-back",
                    "home_location": {"side": "right_module", "shelf_idx": 2, "cell_idx": 3},
                    "inner_dimensions": {"width": 100.0, "height": 100.0, "length": 100.0},
                    "max_items": 4,
                },
            ]
        },
    }


def test_v5_renames_module_side_scan_keys_and_bin_sides(v4_side_config):
    migrated, changes = runner.migrate_config(v4_side_config)
    assert migrated["schema_version"] == 7
    scan = migrated["robot"]["grid"]["scan"]
    assert scan["front_shelf_scout_x_mm"] == 120.0
    assert scan["back_shelf_scout_x_mm"] == 45.0
    assert scan["front_gripper_xy_correction_mm"] == {"x_mm": -1.0, "y_mm": -2.0}
    assert scan["back_gripper_xy_correction_mm"] is None
    assert scan["front_wall_depth_to_gripper_z_model"]["intercept_mm"] == 1.5
    assert scan["back_wall_depth_to_gripper_z_model"] is None
    assert scan["front_bounds_y_max_mm"] == 1250.0
    assert scan["back_bounds_x_min_mm"] == 0.0
    for key in v005.SCAN_KEY_RENAMES:
        assert key not in scan
    assert migrated["storage"]["bins"][0]["home_location"]["side"] == "front_module"
    assert migrated["storage"]["bins"][1]["home_location"]["side"] == "back_module"
    assert "robot.grid.scan.left_gripper_xy_correction_mm -> front_gripper_xy_correction_mm" in changes
    assert "storage bin home_location.side left_module -> front_module" in changes
    assert "storage bin home_location.side right_module -> back_module" in changes
    RootConfig.model_validate(migrated)


def _with_multi_item_pose(config):
    seeded = copy.deepcopy(config)
    parameters = seeded["robot"]["tool"]["parameters"]
    for new, old in v006.SEEDED_KEYS.items():
        parameters[new] = copy.deepcopy(parameters[old])
    return seeded


def test_v6_seeds_the_multi_item_pose_from_pickup_low_and_keeps_existing_values(v4_side_config):
    parameters = v4_side_config["robot"]["tool"]["parameters"]
    parameters["PICKUP_POSITION_L"] = [120, 340]
    parameters["STAGE_EXTENSION_IO_BOX"] = 52.0
    parameters["Z_EXTENSION_IO"] = 163.5
    migrated, changes = runner.migrate_config(v4_side_config)
    seeded = migrated["robot"]["tool"]["parameters"]
    assert seeded["PICKUP_POSITION_MULTI_ITEM"] == [120, 340]
    assert seeded["STAGE_EXTENSION_IO_BOX_MULTI_ITEM"] == 52.0
    assert seeded["Z_EXTENSION_IO_MULTI_ITEM"] == 163.5
    assert "robot.tool.parameters.PICKUP_POSITION_MULTI_ITEM seeded from PICKUP_POSITION_L" in changes
    assert RootConfig.model_validate(migrated).robot.tool.parameters.PICKUP_POSITION_MULTI_ITEM == (120, 340)

    calibrated = {**copy.deepcopy(migrated), "schema_version": 5}
    calibrated["robot"]["tool"]["parameters"]["PICKUP_POSITION_MULTI_ITEM"] = [10, 20]
    remigrated, _ = runner.migrate_config(calibrated)
    assert remigrated["robot"]["tool"]["parameters"]["PICKUP_POSITION_MULTI_ITEM"] == [10, 20]


def test_chain_runs_in_order_and_advances_only_after_success(legacy_config, monkeypatch):
    def third(config):
        assert config["schema_version"] == 4
        config["third_step"] = True
        return config, ["third step"]

    monkeypatch.setattr(runner, "CURRENT_SCHEMA_VERSION", 5)
    monkeypatch.setitem(runner.MIGRATIONS, 5, third)
    result, changes = runner.migrate_config(legacy_config)
    assert result["schema_version"] == 5 and result["third_step"] is True
    assert changes[-3:] == ["schema_version: 3 -> 4", "third step", "schema_version: 4 -> 5"]
    monkeypatch.delitem(runner.MIGRATIONS, 5)
    with pytest.raises(ValueError, match="Missing config migration 4 -> 5"):
        runner.migrate_config(legacy_config)
    assert "schema_version" not in legacy_config


def test_startup_rejects_unmigrated_config_without_writing(tmp_path, legacy_config):
    path = tmp_path / "config.json"
    raw = json.dumps(legacy_config).encode()
    path.write_bytes(raw)
    with pytest.raises(ConfigValidationError, match="requires migration to 7"):
        load_config_file(path)
    assert path.read_bytes() == raw
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize(
    "old",
    list(SCAN_KEY_RENAMES)
    + list(REMOVED_SCAN_KEYS)
    + list(UNUSED_SCAN_KEYS)
    + list(PROMOTED_SCAN_KEYS)
    + list(v005.SCAN_KEY_RENAMES)
    + list(Z_STALL_PROBE_SCAN_KEYS),
)
def test_marking_old_fields_current_does_not_silently_drop_them(legacy_config, old):
    migrated, _ = runner.migrate_config(legacy_config)
    migrated["robot"]["grid"]["scan"][old] = 0
    with pytest.raises(ValueError, match="Legacy laser scan config keys"):
        RootConfig.model_validate(migrated)


@pytest.mark.parametrize(
    "old,new", [("stage_extension_mm", "reference_stage_mm"), ("slope_per_mm", "x_slope_mm_per_mm")]
)
def test_nested_conflicts_are_rejected_and_matching_names_deduplicated(legacy_config, old, new):
    model = legacy_config["robot"]["grid"]["scan"]["left_z_offset_model"]
    model[new] = model[old] + 1
    with pytest.raises(ValueError, match="Conflicting keys"):
        runner.migrate_config(legacy_config)
    model[new] = model[old]
    migrated, _ = runner.migrate_config(legacy_config)
    assert old not in migrated["robot"]["grid"]["scan"]["front_wall_depth_to_gripper_z_model"]


@pytest.mark.parametrize(
    "problem",
    ["conflict", "invalid", "empty_grid", "invalid_grid", "future", "backup", "symlink", "duplicate"],
)
def test_cli_preflights_every_file_before_writing(tmp_path, legacy_config, problem):
    first = tmp_path / "config.a.json"
    second = tmp_path / "config.b.json"
    raw = json.dumps(legacy_config)
    first.write_text(raw)
    bad = copy.deepcopy(legacy_config)
    if problem == "conflict":
        bad["robot"]["grid"]["scan"]["laser_scan_pose_stage_mm"] = 999
    elif problem == "invalid":
        del bad["robot"]["tool"]["parameters"]["Z_MOVE_RPM"]
    elif problem == "empty_grid":
        bad["robot"]["grid"] = ""
    elif problem == "invalid_grid":
        bad["robot"]["grid"] = ["storage.json"]
    elif problem == "future":
        bad["schema_version"] = 999
    second.write_text(json.dumps(bad))
    if problem == "backup":
        second.with_name(second.name + ".schema-v1.bak").write_text("existing rollback copy")
    elif problem == "symlink":
        second.unlink()
        second.symlink_to(first)
    elif problem == "duplicate":
        second.write_text('{"schema_version": 1, "schema_version": 2}')
    assert cli.main([str(tmp_path), "--write"]) == 1
    assert first.read_text() == raw
    assert not first.with_name(first.name + ".schema-v1.bak").exists()
    if problem == "backup":
        assert (
            second.with_name(second.name + ".schema-v1.bak").read_text() == "existing rollback copy"
        )


def test_failed_atomic_replace_keeps_original_and_backup(tmp_path, legacy_config, monkeypatch):
    path = tmp_path / "config.json"
    raw = json.dumps(legacy_config)
    path.write_text(raw)

    def fail_replace(*args):
        raise OSError("simulated replacement failure")

    monkeypatch.setattr(apply.os, "replace", fail_replace)
    assert cli.main([str(path), "--write"]) == 1
    assert path.read_text() == raw
    assert path.with_name(path.name + ".schema-v1.bak").read_text() == raw
    assert len(list(tmp_path.iterdir())) == 2
