import asyncio
from contextlib import contextmanager
import importlib.util
from pathlib import Path
import socket
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pymodbus.exceptions import ModbusIOException


def load_module():
    repo_root = Path(__file__).resolve().parents[4]
    script_path = repo_root / "scripts" / "y_brake_timing_diagnostic.py"
    spec = importlib.util.spec_from_file_location("y_brake_timing_test_module", script_path)
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
            module.SUPPORTED_REGISTER_MAP,
            "--config",
            "test-config.json",
        ]
    )


def snapshot(module, **overrides):
    values = {
        "monotonic_s": 1.0,
        "status": 0,
        "alarm": 0,
        "raw_position": 0,
        "position_mm": 0.0,
        "so2_function": module.DEDICATED_BRAKE_NO,
        "output_state": 2,
        "brake_release_delay_ms": 100,
        "brake_engagement_delay_ms": 100,
        "brake_engagement_speed_threshold": module.BRAKE_SPEED_THRESHOLD,
        "command_pulses_per_revolution": 10000,
        "control_mode": 2,
        "peak_current_setting": 60,
        "encoder_resolution_cpr": 4000,
        "bus_voltage": 240,
    }
    values.update(overrides)
    return module.DriveSnapshot(**values)


def test_commands_and_motion_envelope_are_fixed() -> None:
    module = load_module()
    parser = module.build_parser()
    commands = set(parser._subparsers._group_actions[0].choices)

    assert commands == {"inspect", *module.DELAY_TRANSITIONS, *module.FIXED_MOVES}
    assert {move.target_mm for move in module.FIXED_MOVES.values()} == {10.0, 15.0, 20.0}
    assert module.TEST_Y_MAX_MM == 50.0
    assert module.SHORT_MOVE_RPM == 10
    assert module.SHORT_MOVE_ACCEL == 100
    for command_parser in parser._subparsers._group_actions[0].choices.values():
        for option in ("--unit", "--target-mm", "--max-y-mm", "--rpm", "--accel", "--value"):
            assert option not in command_parser._option_string_actions


def test_harness_has_no_homing_so2_write_or_persistence_command() -> None:
    module = load_module()
    source = (module.REPO_ROOT / "scripts/y_brake_timing_diagnostic.py").read_text()

    assert "0x2211" not in source
    assert "SAVE_PARAMETERS" not in source
    assert "motor.home" not in source
    assert "write_so2" not in source
    assert "transition-to-brk" not in source


def test_device_wrapper_forces_the_preloaded_image() -> None:
    module = load_module()
    wrapper = (module.REPO_ROOT / "scripts/run_y_brake_timing_stage.sh").read_text()
    assert "run --pull never --rm" in wrapper


def test_every_delay_transition_changes_exactly_one_delay_between_100_and_250() -> None:
    module = load_module()
    for transition in module.DELAY_TRANSITIONS.values():
        changes = [before != after for before, after in zip(transition.before, transition.after)]
        assert changes.count(True) == 1
        assert set(transition.before + transition.after) <= {100, 250}


@pytest.mark.parametrize("command", ["set-release-250", "restore-both-release-100"])
def test_delay_writes_require_exact_confirmation(command: str) -> None:
    module = load_module()
    args = parse(module, command, "--confirm", "wrong")
    with pytest.raises(ValueError, match="delay-write confirmation mismatch"):
        module.validate_args(args)


def test_moves_require_exact_confirmation() -> None:
    module = load_module()
    args = parse(module, "move-release-250", "--confirm", "wrong")
    with pytest.raises(ValueError, match="move confirmation mismatch"):
        module.validate_args(args)


def test_refuse_if_api_is_live_rejects_before_hardware_access(monkeypatch) -> None:
    module = load_module()
    connection = Mock()
    monkeypatch.setattr(module.socket, "create_connection", Mock(return_value=connection))
    with pytest.raises(RuntimeError, match="normal API appears to be listening"):
        module.refuse_if_api_is_live("127.0.0.1", 8000)
    connection.close.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("probe_error", [socket.timeout("timed out"), OSError("failed")])
async def test_api_probe_uncertainty_refuses_before_controller(monkeypatch, probe_error) -> None:
    module = load_module()
    args = parse(module, "inspect", "--expect", "baseline-100", "--samples", "1")
    make_controller = Mock()

    @contextmanager
    def lock(_path):
        yield

    monkeypatch.setattr(module, "acquire_ownership_lock", lock)
    monkeypatch.setattr(module.socket, "create_connection", Mock(side_effect=probe_error))
    monkeypatch.setattr(module, "make_controller", make_controller)
    with pytest.raises(RuntimeError, match="cannot verify normal API is stopped"):
        await module.async_main(args)
    make_controller.assert_not_called()


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"position_mm": 50.001}, "hardcoded diagnostic window"),
        ({"status": 1 << 2}, "motion running"),
        ({"alarm": 1}, "alarm is active"),
    ],
)
def test_safe_snapshot_rejects_unsafe_state(override, message) -> None:
    module = load_module()
    with pytest.raises(RuntimeError, match=message):
        module.require_safe_snapshot(snapshot(module, **override))


@pytest.mark.parametrize(
    ("override", "field"),
    [
        ({"so2_function": 0xA1}, "so2_function"),
        ({"brake_release_delay_ms": 250}, "brake_release_delay_ms"),
        ({"command_pulses_per_revolution": 2000}, "command_pulses_per_revolution"),
        ({"peak_current_setting": 61}, "peak_current_setting"),
    ],
)
def test_profile_gate_rejects_unexpected_live_state(override, field) -> None:
    module = load_module()
    with pytest.raises(RuntimeError, match=field):
        module.require_live_profile(snapshot(module, **override), expected_delays=(100, 100))


@pytest.mark.asyncio
async def test_delay_transition_writes_once_with_transport_retries_disabled(monkeypatch) -> None:
    module = load_module()
    before = snapshot(module)
    after = snapshot(module, brake_release_delay_ms=250)

    async def successful_write(**_kwargs):
        assert client.ctx.retries == 0
        return Mock(isError=Mock(return_value=False))

    client = SimpleNamespace(
        connected=True,
        ctx=SimpleNamespace(retries=3),
        write_register=AsyncMock(side_effect=successful_write),
    )
    controller = SimpleNamespace(lock=asyncio.Lock(), client=client)
    args = SimpleNamespace(
        command="set-release-250",
        arm_delay_s=5.0,
        samples=2,
        sample_interval_ms=10.0,
    )
    monkeypatch.setattr(
        module,
        "require_stationary_healthy_y",
        AsyncMock(side_effect=[before, before]),
    )
    monkeypatch.setattr(module, "capture_snapshot", AsyncMock(return_value=after))
    monkeypatch.setattr(module, "arm_countdown", AsyncMock())
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())

    assert await module.run_delay_transition(args, controller, SimpleNamespace()) == 0
    client.write_register.assert_awaited_once_with(
        address=module.REGISTERS["brake_release_delay_ms"][0],
        value=250,
        device_id=module.Y_AXIS_UNIT,
    )
    assert client.ctx.retries == 3


@pytest.mark.asyncio
async def test_ambiguous_delay_write_reads_back_without_retransmitting(monkeypatch) -> None:
    module = load_module()

    async def lost_response(**_kwargs):
        assert client.ctx.retries == 0
        raise TimeoutError("response lost")

    client = SimpleNamespace(
        connected=True,
        ctx=SimpleNamespace(retries=3),
        write_register=AsyncMock(side_effect=lost_response),
    )
    controller = SimpleNamespace(lock=asyncio.Lock(), client=client)
    args = SimpleNamespace(
        command="set-release-250",
        arm_delay_s=5.0,
        samples=1,
        sample_interval_ms=10.0,
    )
    before = snapshot(module)
    after = snapshot(module, brake_release_delay_ms=250)
    monkeypatch.setattr(
        module,
        "require_stationary_healthy_y",
        AsyncMock(side_effect=[before, before]),
    )
    monkeypatch.setattr(module, "capture_snapshot", AsyncMock(return_value=after))
    monkeypatch.setattr(module, "read_register", AsyncMock(return_value=(250,)))
    monkeypatch.setattr(module, "arm_countdown", AsyncMock())

    assert await module.run_delay_transition(args, controller, SimpleNamespace()) == 0
    client.write_register.assert_awaited_once()
    module.read_register.assert_awaited_once_with(controller, "brake_release_delay_ms")


@pytest.mark.asyncio
async def test_transition_rejects_wrong_predecessor_before_write(monkeypatch) -> None:
    module = load_module()
    wrong = snapshot(module, brake_release_delay_ms=250)
    controller = SimpleNamespace(lock=asyncio.Lock(), client=Mock())
    args = SimpleNamespace(command="set-release-250", arm_delay_s=5.0)
    monkeypatch.setattr(module, "require_stationary_healthy_y", AsyncMock(return_value=wrong))
    with pytest.raises(RuntimeError, match="required timing-test state"):
        await module.run_delay_transition(args, controller, SimpleNamespace())


@pytest.mark.asyncio
async def test_fixed_move_uses_hardcoded_target_and_speed(monkeypatch) -> None:
    module = load_module()
    baseline = snapshot(module, brake_release_delay_ms=250, position_mm=5.0)
    final = snapshot(module, brake_release_delay_ms=250, position_mm=10.0)
    motor = Mock(move_config=AsyncMock(), move_to_position=AsyncMock(), stop=AsyncMock())
    monkeypatch.setattr(
        module,
        "require_stationary_healthy_y",
        AsyncMock(side_effect=[baseline, baseline, baseline, final]),
    )
    monkeypatch.setattr(module, "arm_countdown", AsyncMock())
    monkeypatch.setattr(module, "Motor", Mock(return_value=motor))
    args = SimpleNamespace(command="move-release-250", arm_delay_s=5.0)

    assert await module.run_fixed_move(args, SimpleNamespace(), SimpleNamespace()) == 0
    motor.move_to_position.assert_awaited_once_with(10.0, rpm=10, accel=100)
    motor.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_fixed_move_rejects_nonascending_target_before_motor_creation(monkeypatch) -> None:
    module = load_module()
    baseline = snapshot(module, brake_release_delay_ms=250, position_mm=9.7)
    motor = Mock()
    monkeypatch.setattr(module, "require_stationary_healthy_y", AsyncMock(return_value=baseline))
    monkeypatch.setattr(module, "Motor", motor)
    args = SimpleNamespace(command="move-release-250", arm_delay_s=5.0)
    with pytest.raises(RuntimeError, match="move away from the lower hard stop"):
        await module.run_fixed_move(args, SimpleNamespace(), SimpleNamespace())
    motor.assert_not_called()


@pytest.mark.asyncio
async def test_fixed_move_cancellation_stops_before_trigger(monkeypatch) -> None:
    module = load_module()
    baseline = snapshot(module, brake_release_delay_ms=250, position_mm=5.0)
    motor = Mock(
        move_config=AsyncMock(side_effect=asyncio.CancelledError()),
        move_to_position=AsyncMock(),
        stop=AsyncMock(),
    )
    monkeypatch.setattr(
        module,
        "require_stationary_healthy_y",
        AsyncMock(side_effect=[baseline, baseline]),
    )
    monkeypatch.setattr(module, "arm_countdown", AsyncMock())
    monkeypatch.setattr(module, "Motor", Mock(return_value=motor))
    args = SimpleNamespace(command="move-release-250", arm_delay_s=5.0)
    with pytest.raises(asyncio.CancelledError):
        await module.run_fixed_move(args, SimpleNamespace(), SimpleNamespace())
    motor.stop.assert_awaited_once()
    motor.move_to_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_fail_closed_controller_propagates_converted_cancellation_without_retry() -> None:
    module = load_module()
    transaction = AsyncMock()
    controller = module.FailClosedSharedModbusController(
        port="/dev/test", baudrate=115200, timeout=1.0
    )
    controller.client = SimpleNamespace(connected=True, read_holding_registers=transaction)
    controller._reconnect = AsyncMock()

    async def cancelled_transaction(**_kwargs):
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        raise ModbusIOException("transaction cancelled")

    transaction.side_effect = cancelled_transaction
    task = asyncio.create_task(controller.read_holding_register_block(module.Y_AXIS_UNIT, 0x1003))
    with pytest.raises(asyncio.CancelledError):
        await task
    transaction.assert_awaited_once()
    controller._reconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_main_applies_guards_before_serial_connect(monkeypatch) -> None:
    module = load_module()
    args = parse(module, "inspect", "--expect", "baseline-100", "--samples", "1")
    events = []
    controller = Mock(
        connect=AsyncMock(side_effect=lambda: events.append("connect")),
        close=AsyncMock(side_effect=lambda: events.append("close")),
    )

    @contextmanager
    def lock(_path):
        events.append("lock")
        yield

    monkeypatch.setattr(module, "acquire_ownership_lock", lock)
    monkeypatch.setattr(module, "refuse_if_api_is_live", lambda *_: events.append("api"))
    monkeypatch.setattr(
        module, "load_y_motor_params", lambda _: events.append("config") or SimpleNamespace()
    )
    monkeypatch.setattr(module, "make_controller", lambda _: controller)
    monkeypatch.setattr(
        module,
        "run_inspect",
        AsyncMock(side_effect=lambda *_: events.append("inspect") or 0),
    )

    assert await module.async_main(args) == 0
    assert events == ["lock", "api", "config", "connect", "inspect", "close"]
