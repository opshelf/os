import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock, call

import pytest


def load_module():
    repo_root = Path(__file__).resolve().parents[4]
    script_dir = repo_root / "scripts"
    if str(script_dir) not in sys.path:
        sys.path.insert(0, str(script_dir))
    script_path = script_dir / "y_home_zone_diagnostic.py"
    spec = importlib.util.spec_from_file_location("y_home_zone_test_module", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def parse(module, command: str, *arguments: str):
    return module.build_parser().parse_args(
        [
            command,
            *arguments,
            "--register-map",
            module.timing.SUPPORTED_REGISTER_MAP,
            "--config",
            "test-config.json",
        ]
    )


def drive(module, position_mm: float):
    return module.timing.DriveSnapshot(
        monotonic_s=1.0,
        status=0,
        alarm=0,
        raw_position=int(position_mm * 250),
        position_mm=position_mm,
        so2_function=module.timing.DEDICATED_BRAKE_NO,
        output_state=2,
        brake_release_delay_ms=250,
        brake_engagement_delay_ms=250,
        brake_engagement_speed_threshold=module.timing.BRAKE_SPEED_THRESHOLD,
        command_pulses_per_revolution=10000,
        control_mode=2,
        peak_current_setting=60,
        encoder_resolution_cpr=4000,
        bus_voltage=240,
    )


def gpio(module, *, pin0_high: bool, port1: int = 0):
    return module.GpioSnapshot(
        monotonic_s=1.0,
        port0=1 if pin0_high else 0,
        port1=port1,
        pin0_high=pin0_high,
    )


def expander(module):
    return module.ExpanderTarget(
        bus=1,
        address=0x20,
        configured_pin_zero_name="x_limit_min",
        configured_y_min_name="y_limit_min",
        configured_y_min_pin=4,
    )


def test_commands_and_motion_envelope_are_fixed() -> None:
    module = load_module()
    parser = module.build_parser()
    commands = set(parser._subparsers._group_actions[0].choices)

    assert commands == {"inspect", "scan-zone"}
    assert module.START_Y_MIN_MM == -1.0
    assert module.START_Y_MAX_MM == 1.0
    assert module.MAX_Y_MM == 10.0
    assert module.MOVE_RPM == 10
    assert module.MOVE_ACCEL == 100
    scan_parser = parser._subparsers._group_actions[0].choices["scan-zone"]
    for option in ("--pin", "--target-mm", "--max-y-mm", "--rpm", "--accel"):
        assert option not in scan_parser._option_string_actions


def test_harness_has_no_homing_so2_write_or_persistence_command() -> None:
    module = load_module()
    source = (module.SCRIPT_DIR / "y_home_zone_diagnostic.py").read_text()

    assert "motor.home" not in source
    assert "0x2211" not in source
    assert "SAVE_PARAMETERS" not in source
    assert "set_brake_output_function" not in source


def test_device_wrapper_forces_preloaded_image_and_no_dependencies() -> None:
    module = load_module()
    wrapper = (module.SCRIPT_DIR / "run_y_home_zone_stage.sh").read_text()

    assert "run --pull never --no-deps --rm" in wrapper
    assert "config.development.json" in wrapper
    assert "config.json" in wrapper
    assert 'docker image inspect "$api_image"' in wrapper
    production_case = wrapper.split("production)", 1)[1].split(";;", 1)[0]
    assert "docker-compose.dev.yml" not in production_case


def test_scan_requires_exact_confirmation() -> None:
    module = load_module()
    args = parse(module, "scan-zone", "--confirm", "wrong")

    with pytest.raises(ValueError, match="scan confirmation mismatch"):
        module.validate_args(args)


@pytest.mark.parametrize("position_mm", [-1.001, 1.001, float("nan")])
def test_scan_start_rejects_position_outside_near_home_window(position_mm: float) -> None:
    module = load_module()

    with pytest.raises(RuntimeError, match="must start close"):
        module.require_scan_start(position_mm)


def test_target_generators_never_exceed_ceiling_or_go_below_start() -> None:
    module = load_module()

    away = module.away_targets(-0.04)
    returning = module.return_targets(3.0, -0.04)

    assert away[0] == 1.0
    assert away[-1] == module.MAX_Y_MM
    assert max(away) <= module.MAX_Y_MM
    assert returning[-1] == -0.04
    assert min(returning) >= -0.04
    assert returning == sorted(returning, reverse=True)


@pytest.mark.asyncio
async def test_scan_stops_return_when_pin_zero_reasserts(monkeypatch) -> None:
    module = load_module()
    baseline_drive = drive(module, 0.0)
    baseline_gpio = gpio(module, pin0_high=False)
    controller = SimpleNamespace()
    motor = Mock(move_config=AsyncMock(), stop=AsyncMock())
    monkeypatch.setattr(
        module.timing,
        "require_stationary_healthy_y",
        AsyncMock(side_effect=[baseline_drive, baseline_drive, baseline_drive]),
    )
    monkeypatch.setattr(module.timing, "require_live_profile", Mock())
    monkeypatch.setattr(module.timing, "require_no_position_change", Mock())
    monkeypatch.setattr(module.timing, "arm_countdown", AsyncMock())
    monkeypatch.setattr(
        module,
        "capture_stable_gpio",
        AsyncMock(side_effect=[baseline_gpio, baseline_gpio]),
    )
    monkeypatch.setattr(module, "Motor", Mock(return_value=motor))

    async def fake_move_and_capture(*_args, target_mm: float, phase: str, **_kwargs):
        if phase == "away":
            return drive(module, target_mm), gpio(module, pin0_high=target_mm >= 2.0)
        return drive(module, target_mm), gpio(module, pin0_high=target_mm > 0.5)

    move = AsyncMock(side_effect=fake_move_and_capture)
    monkeypatch.setattr(module, "move_and_capture", move)
    args = SimpleNamespace(command="scan-zone", arm_delay_s=5.0)

    assert (
        await module.run_scan(
            args,
            controller,
            SimpleNamespace(),
            SimpleNamespace(),
            expander(module),
        )
        == 0
    )
    motor.move_config.assert_awaited_once()
    motor.stop.assert_not_awaited()
    assert [item.kwargs["phase"] for item in move.await_args_list] == [
        "away",
        "away",
        "return",
        "return",
        "return",
    ]
    assert [item.kwargs["target_mm"] for item in move.await_args_list] == [
        1.0,
        2.0,
        1.5,
        1.0,
        0.5,
    ]


@pytest.mark.asyncio
async def test_scan_leaves_y_at_upper_bound_when_pin_never_clears(monkeypatch) -> None:
    module = load_module()
    baseline_drive = drive(module, 0.0)
    baseline_gpio = gpio(module, pin0_high=False)
    motor = Mock(move_config=AsyncMock(), stop=AsyncMock())
    monkeypatch.setattr(
        module.timing,
        "require_stationary_healthy_y",
        AsyncMock(side_effect=[baseline_drive, baseline_drive, baseline_drive]),
    )
    monkeypatch.setattr(module.timing, "require_live_profile", Mock())
    monkeypatch.setattr(module.timing, "require_no_position_change", Mock())
    monkeypatch.setattr(module.timing, "arm_countdown", AsyncMock())
    monkeypatch.setattr(
        module,
        "capture_stable_gpio",
        AsyncMock(side_effect=[baseline_gpio, baseline_gpio]),
    )
    monkeypatch.setattr(module, "Motor", Mock(return_value=motor))

    async def never_changes(*_args, target_mm: float, **_kwargs):
        return drive(module, target_mm), baseline_gpio

    move = AsyncMock(side_effect=never_changes)
    monkeypatch.setattr(module, "move_and_capture", move)

    with pytest.raises(RuntimeError, match="left at the safe upper scan bound"):
        await module.run_scan(
            SimpleNamespace(command="scan-zone", arm_delay_s=5.0),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            expander(module),
        )

    assert move.await_count == 10
    assert move.await_args_list[-1] == call(
        motor,
        ANY,
        ANY,
        ANY,
        target_mm=10.0,
        phase="away",
    )
    motor.stop.assert_awaited_once()
