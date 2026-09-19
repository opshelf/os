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


def gpio(module, *, y_min_high: bool, y_max_high: bool = False, port1: int = 0):
    port0 = 0
    if y_min_high:
        port0 |= 1 << module.EXPECTED_RAW_YMIN_PIN
    if y_max_high:
        port0 |= 1 << module.EXPECTED_RAW_YMAX_PIN
    return module.GpioSnapshot(
        monotonic_s=1.0,
        port0=port0,
        port1=port1,
        y_min_high=y_min_high,
        y_max_high=y_max_high,
    )


def expander(module):
    return module.ExpanderTarget(
        bus=1,
        address=0x20,
        configured_y_min_name="y_lim_min",
        configured_y_min_pin=4,
        configured_y_max_name="y_lim_max",
        configured_y_max_pin=5,
    )


def test_commands_and_motion_envelope_are_fixed() -> None:
    module = load_module()
    parser = module.build_parser()
    commands = set(parser._subparsers._group_actions[0].choices)

    assert commands == {
        "inspect",
        "scan-zone",
        "move-away-100",
        "scan-toward-home",
        "scan-confirmed-limits",
    }
    assert module.START_Y_MIN_MM == -1.0
    assert module.START_Y_MAX_MM == 1.0
    assert module.MAX_Y_MM == 10.0
    assert module.EXTENDED_AWAY_TARGET_MM == 100.0
    assert module.EXTENDED_AWAY_START_MIN_MM == 9.0
    assert module.EXTENDED_AWAY_START_MAX_MM == 11.0
    assert module.HOMEWARD_START_MIN_MM == 99.0
    assert module.HOMEWARD_START_MAX_MM == 101.0
    assert module.HOMEWARD_TARGETS_MM[-1] == 0.0
    assert min(module.HOMEWARD_TARGETS_MM) == 0.0
    assert module.EXPECTED_RAW_YMIN_PIN == 4
    assert module.EXPECTED_RAW_YMAX_PIN == 5
    assert module.MOVE_RPM == 10
    assert module.MOVE_ACCEL == 100
    assert module.timing.REGISTERS["control_mode"] == (0x0003, 2)
    scan_parser = parser._subparsers._group_actions[0].choices["scan-zone"]
    for option in ("--pin", "--target-mm", "--max-y-mm", "--rpm", "--accel"):
        assert option not in scan_parser._option_string_actions
        assert option not in parser._subparsers._group_actions[0].choices[
            "move-away-100"
        ]._option_string_actions
        assert option not in parser._subparsers._group_actions[0].choices[
            "scan-toward-home"
        ]._option_string_actions
        assert option not in parser._subparsers._group_actions[0].choices[
            "scan-confirmed-limits"
        ]._option_string_actions


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
    assert "docker-compose.dev.yml" in wrapper


def test_scan_requires_exact_confirmation() -> None:
    module = load_module()
    args = parse(module, "scan-zone", "--confirm", "wrong")

    with pytest.raises(ValueError, match="scan confirmation mismatch"):
        module.validate_args(args)


def test_extended_move_requires_exact_confirmation() -> None:
    module = load_module()
    args = parse(module, "move-away-100", "--confirm", "wrong")

    with pytest.raises(ValueError, match="100 mm move confirmation mismatch"):
        module.validate_args(args)


def test_homeward_scan_requires_exact_confirmation() -> None:
    module = load_module()
    args = parse(module, "scan-toward-home", "--confirm", "wrong")

    with pytest.raises(ValueError, match="homeward scan confirmation mismatch"):
        module.validate_args(args)


def test_confirmed_limit_scan_requires_exact_confirmation() -> None:
    module = load_module()
    args = parse(module, "scan-confirmed-limits", "--confirm", "wrong")

    with pytest.raises(ValueError, match="confirmed limit scan confirmation mismatch"):
        module.validate_args(args)


@pytest.mark.parametrize("position_mm", [-1.001, 1.001, float("nan")])
def test_scan_start_rejects_position_outside_near_home_window(position_mm: float) -> None:
    module = load_module()

    with pytest.raises(RuntimeError, match="must start close"):
        module.require_scan_start(position_mm)


@pytest.mark.parametrize("position_mm", [8.999, 11.001, float("nan")])
def test_extended_move_rejects_position_outside_10mm_window(
    position_mm: float,
) -> None:
    module = load_module()

    with pytest.raises(RuntimeError, match="must start at the completed 10 mm"):
        module.require_extended_move_start(position_mm)


@pytest.mark.parametrize("position_mm", [98.999, 101.001, float("nan")])
def test_homeward_scan_rejects_position_outside_100mm_window(
    position_mm: float,
) -> None:
    module = load_module()

    with pytest.raises(RuntimeError, match="must start at the completed 100 mm"):
        module.require_homeward_scan_start(position_mm)


@pytest.mark.asyncio
async def test_extended_move_is_fixed_to_100mm_and_reads_both_expanders(
    monkeypatch,
) -> None:
    module = load_module()
    start_drive = drive(module, 10.0)
    final_drive = drive(module, 100.0)
    initial_primary = gpio(module, y_min_high=False, port1=6)
    final_primary = gpio(module, y_min_high=True, port1=6)
    initial_secondary = gpio(module, y_min_high=False, port1=1)
    final_secondary = gpio(module, y_min_high=False, port1=3)
    motor = Mock(move_config=AsyncMock(), stop=AsyncMock())
    monkeypatch.setattr(
        module,
        "require_extended_stationary_healthy_y",
        AsyncMock(side_effect=[start_drive, start_drive, start_drive]),
    )
    monkeypatch.setattr(module.timing, "require_live_profile", Mock())
    monkeypatch.setattr(module.timing, "require_no_position_change", Mock())
    monkeypatch.setattr(module.timing, "arm_countdown", AsyncMock())
    monkeypatch.setattr(
        module, "capture_stable_gpio", AsyncMock(return_value=initial_primary)
    )
    other = AsyncMock(side_effect=[initial_secondary, final_secondary])
    monkeypatch.setattr(module, "capture_other_expander", other)
    move = AsyncMock(return_value=(final_drive, final_primary))
    monkeypatch.setattr(module, "move_extended_and_capture", move)
    monkeypatch.setattr(module, "Motor", Mock(return_value=motor))

    result = await module.run_extended_move(
        SimpleNamespace(command="move-away-100", arm_delay_s=5.0),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        expander(module),
    )

    assert result == 0
    motor.move_config.assert_awaited_once()
    motor.stop.assert_not_awaited()
    move.assert_awaited_once_with(motor, ANY, ANY, ANY)
    assert other.await_args_list == [
        call(1, module.SECONDARY_EXPANDER_ADDRESS),
        call(1, module.SECONDARY_EXPANDER_ADDRESS),
    ]


@pytest.mark.asyncio
async def test_homeward_scan_stops_at_first_pin_zero_transition(monkeypatch) -> None:
    module = load_module()
    start_drive = drive(module, 100.0)
    baseline_primary = gpio(module, y_min_high=False, port1=6)
    baseline_secondary = gpio(module, y_min_high=False, port1=1)
    motor = Mock(move_config=AsyncMock(), stop=AsyncMock())
    monkeypatch.setattr(
        module,
        "require_extended_stationary_healthy_y",
        AsyncMock(side_effect=[start_drive, start_drive, start_drive]),
    )
    monkeypatch.setattr(module.timing, "require_live_profile", Mock())
    monkeypatch.setattr(module.timing, "require_no_position_change", Mock())
    monkeypatch.setattr(module.timing, "arm_countdown", AsyncMock())
    monkeypatch.setattr(
        module, "capture_stable_gpio", AsyncMock(return_value=baseline_primary)
    )
    other = AsyncMock(return_value=baseline_secondary)
    monkeypatch.setattr(module, "capture_other_expander", other)
    monkeypatch.setattr(module, "Motor", Mock(return_value=motor))

    async def fake_move(*_args, target_mm: float, **_kwargs):
        return drive(module, target_mm), gpio(
            module,
            y_min_high=target_mm <= 0.5,
            port1=6,
        )

    move = AsyncMock(side_effect=fake_move)
    monkeypatch.setattr(module, "move_homeward_and_capture", move)

    result = await module.run_homeward_scan(
        SimpleNamespace(command="scan-toward-home", arm_delay_s=5.0),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        expander(module),
    )

    assert result == 0
    motor.move_config.assert_awaited_once()
    motor.stop.assert_not_awaited()
    assert [item.kwargs["target_mm"] for item in move.await_args_list] == [
        50.0,
        20.0,
        10.0,
        5.0,
        2.0,
        1.0,
        0.5,
    ]
    assert other.await_count == 8


@pytest.mark.asyncio
async def test_confirmed_limit_scan_moves_to_100_and_stops_when_p04_reasserts(
    monkeypatch,
) -> None:
    module = load_module()
    home_drive = drive(module, 0.0)
    away_drive = drive(module, 100.0)
    home_gpio = gpio(module, y_min_high=True, y_max_high=False, port1=6)
    away_gpio = gpio(module, y_min_high=False, y_max_high=False, port1=6)
    state = {"position_mm": 0.0}
    logical_y_min = SimpleNamespace(
        read=lambda: state["position_mm"] <= 0.5,
    )
    logical_y_max = SimpleNamespace(read=lambda: False)
    motor = Mock(move_config=AsyncMock(), stop=AsyncMock())
    monkeypatch.setattr(
        module,
        "require_extended_stationary_healthy_y",
        AsyncMock(side_effect=[home_drive, home_drive, home_drive]),
    )
    monkeypatch.setattr(module.timing, "require_live_profile", Mock())
    monkeypatch.setattr(module.timing, "require_no_position_change", Mock())
    monkeypatch.setattr(module.timing, "arm_countdown", AsyncMock())
    monkeypatch.setattr(
        module,
        "capture_stable_gpio",
        AsyncMock(side_effect=[home_gpio, home_gpio]),
    )

    async def fake_away(*_args, **_kwargs):
        state["position_mm"] = 100.0
        return away_drive, away_gpio

    move_away = AsyncMock(side_effect=fake_away)
    monkeypatch.setattr(module, "move_extended_and_capture", move_away)
    monkeypatch.setattr(module, "Motor", Mock(return_value=motor))

    async def fake_return(*_args, target_mm: float, **_kwargs):
        state["position_mm"] = target_mm
        return drive(module, target_mm), gpio(
            module,
            y_min_high=target_mm <= 0.5,
            y_max_high=False,
            port1=6,
        )

    move_homeward = AsyncMock(side_effect=fake_return)
    monkeypatch.setattr(module, "move_homeward_and_capture", move_homeward)

    result = await module.run_confirmed_limit_scan(
        SimpleNamespace(command="scan-confirmed-limits", arm_delay_s=5.0),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        expander(module),
        logical_y_min,
        logical_y_max,
    )

    assert result == 0
    motor.move_config.assert_awaited_once()
    motor.stop.assert_not_awaited()
    move_away.assert_awaited_once_with(motor, ANY, ANY, ANY)
    assert [item.kwargs["target_mm"] for item in move_homeward.await_args_list] == [
        50.0,
        20.0,
        10.0,
        5.0,
        2.0,
        1.0,
        0.5,
    ]


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
    baseline_gpio = gpio(module, y_min_high=False)
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
            return drive(module, target_mm), gpio(
                module, y_min_high=target_mm >= 2.0
            )
        return drive(module, target_mm), gpio(
            module, y_min_high=target_mm > 0.5
        )

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
    baseline_gpio = gpio(module, y_min_high=False)
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
