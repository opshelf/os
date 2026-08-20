import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def load_module():
    repo_root = Path(__file__).resolve().parents[4]
    script_path = repo_root / "scripts" / "y_first_home_diagnostic.py"
    spec = importlib.util.spec_from_file_location("y_first_home_test_module", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def completed_trace(module, path: Path, **overrides) -> None:
    snapshot = {
        "position_mm": 5.0,
        "status": 0,
        "alarm": 0,
        "so2_function": 0x24,
    }
    snapshot.update(overrides)
    events = [
        {"event": "inspection_sample", "snapshot": snapshot},
        {"event": "inspection_complete", "samples": 1, "writes": 0},
    ]
    path.write_text("\n".join(json.dumps(event) for event in events))


def safe_status(**overrides):
    payload = {
        "current_command": None,
        "queue_length": 0,
        "is_crashed": False,
        "is_emergency_stop_active": False,
        "robot_status": {
            "gantry": {"y_axis": {"position": 5.0}},
            "safety": {
                "active": False,
                "motion_blocked": False,
                "awaiting_ack": False,
                "active_sources": [],
                "bypassed": False,
                "component_bypasses": {"cabinet_doors": False},
            },
        },
    }
    payload.update(overrides)
    return payload


def test_home_commands_have_fixed_settle_times_and_no_api_target_override() -> None:
    module = load_module()
    parser = module.build_parser()
    commands = parser._subparsers._group_actions[0].choices
    assert set(commands) == {"check", "home-immediate", "home-after-5s"}
    assert module.HOME_SETTLE_SECONDS == {"home-immediate": 0.0, "home-after-5s": 5.0}
    assert module.API_BASE == "http://127.0.0.1:8000"
    for subparser in commands.values():
        assert "--base-url" not in subparser._option_string_actions
        assert "--settle-seconds" not in subparser._option_string_actions


def test_status_stream_requires_and_returns_safety_plus_y_state(monkeypatch) -> None:
    module = load_module()
    payload = safe_status()
    lines = iter(
        [
            b"event: init_progress\n",
            b'data: {"phase":"ready"}\n',
            ("data: " + json.dumps(payload) + "\n").encode(),
        ]
    )

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def readline(self):
            return next(lines, b"")

    monkeypatch.setattr(module, "urlopen", Mock(return_value=Response()))
    assert module.request_robot_status("token") == payload


def test_init_progress_stream_returns_structured_snapshot(monkeypatch) -> None:
    module = load_module()
    payload = {
        "phase": "degraded",
        "components": [
            {"component": "y_axis", "phase": "ready", "reason": None},
            {"component": "tool_camera", "phase": "failed", "reason": "no frames within 15s"},
        ],
    }
    lines = iter(
        [
            b"event: init_progress\n",
            ("data: " + json.dumps(payload) + "\n").encode(),
        ]
    )

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def readline(self):
            return next(lines, b"")

    monkeypatch.setattr(module, "urlopen", Mock(return_value=Response()))
    assert module.request_init_progress("token") == payload


def test_health_allows_only_expected_unconfigured_tool_camera(monkeypatch) -> None:
    module = load_module()
    progress = {
        "phase": "degraded",
        "components": [
            {"component": "y_axis", "phase": "ready", "reason": None},
            {"component": "tool_camera", "phase": "failed", "reason": "no frames within 15s"},
        ],
    }
    monkeypatch.setattr(module, "request_init_progress", Mock(return_value=progress))
    accepted = module.validate_api_health(
        "token",
        {"status": "ok", "mode": "development", "robot_status": "degraded", "robot_phase": "degraded"},
    )
    assert accepted == ["tool_camera"]


@pytest.mark.parametrize(
    "components",
    [
        [{"component": "y_axis", "phase": "failed", "reason": "timeout"}],
        [{"component": "tool_camera", "phase": "failed", "reason": "wrong reason"}],
        [
            {"component": "tool_camera", "phase": "failed", "reason": "no frames within 15s"},
            {"component": "motor_bus", "phase": "connecting", "reason": None},
        ],
    ],
)
def test_health_rejects_any_other_degradation(monkeypatch, components) -> None:
    module = load_module()
    monkeypatch.setattr(
        module,
        "request_init_progress",
        Mock(return_value={"phase": "degraded", "components": components}),
    )
    with pytest.raises(RuntimeError, match="not limited"):
        module.validate_api_health(
            "token",
            {"status": "ok", "mode": "development", "robot_status": "degraded", "robot_phase": "degraded"},
        )


def test_script_contains_no_restart_config_write_ack_or_global_bypass_endpoint() -> None:
    module = load_module()
    source = Path(module.__file__).read_text()
    assert '"PUT"' not in source
    assert "/api/system/restart" not in source
    assert "/safety/acknowledge" not in source
    assert '"/api/robot/safety/override",' not in source
    assert source.count('"/api/robot/safety/override/component"') == 1
    assert "docker" not in source


def test_home_requires_exact_confirmation() -> None:
    module = load_module()
    args = module.build_parser().parse_args(
        ["home-immediate", "--position-trace", "trace.jsonl", "--confirm", "wrong"]
    )
    with pytest.raises(ValueError, match="home confirmation mismatch"):
        module.validate_args(args)


def test_recent_position_evidence_is_accepted(tmp_path: Path) -> None:
    module = load_module()
    trace = tmp_path / "trace.jsonl"
    completed_trace(module, trace)
    evidence = module.load_recent_position_evidence(trace)
    assert evidence["position_mm"] == 5.0
    assert evidence["so2_function"] == 0x24


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"position_mm": 50.001}, "outside"),
        ({"position_mm": -1.001}, "outside"),
        ({"status": 1 << 2}, "stationary"),
        ({"alarm": 1}, "alarm"),
        ({"so2_function": 0xA1}, "SO2"),
    ],
)
def test_position_evidence_rejects_unsafe_snapshot(tmp_path: Path, overrides, message) -> None:
    module = load_module()
    trace = tmp_path / "trace.jsonl"
    completed_trace(module, trace, **overrides)
    with pytest.raises(RuntimeError, match=message):
        module.load_recent_position_evidence(trace)


def test_position_evidence_requires_completed_read_only_inspection(tmp_path: Path) -> None:
    module = load_module()
    trace = tmp_path / "trace.jsonl"
    trace.write_text(
        json.dumps(
            {
                "event": "inspection_sample",
                "snapshot": {"position_mm": 5, "status": 0, "alarm": 0, "so2_function": 0x24},
            }
        )
    )
    with pytest.raises(RuntimeError, match="completed read-only"):
        module.load_recent_position_evidence(trace)


def test_position_evidence_rejects_any_later_harness_event(tmp_path: Path) -> None:
    module = load_module()
    trace = tmp_path / "trace.jsonl"
    completed_trace(module, trace)
    with trace.open("a") as output:
        output.write('\n{"event":"fatal","error":"later failure"}')
    with pytest.raises(RuntimeError, match="completed read-only"):
        module.load_recent_position_evidence(trace)


def test_position_evidence_rejects_stale_trace(tmp_path: Path) -> None:
    module = load_module()
    trace = tmp_path / "trace.jsonl"
    completed_trace(module, trace)
    stale = module.time.time() - module.MAX_POSITION_TRACE_AGE_SECONDS - 1
    os.utime(trace, (stale, stale))
    with pytest.raises(RuntimeError, match="no older"):
        module.load_recent_position_evidence(trace)


def test_api_preflight_requires_development_ready_home_disabled_and_y_unit(monkeypatch) -> None:
    module = load_module()
    responses = iter(
        [
            {"status": "ok", "mode": "development", "robot_status": "ok", "robot_phase": "ready"},
            {"home_on_init": False},
            {"tool": [{"name": "y_axis", "motor_id": 2}]},
        ]
    )
    request = Mock(side_effect=lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr(module, "request_json", request)
    monkeypatch.setattr(module, "request_robot_status", Mock(return_value=safe_status()))
    module.api_preflight("token")
    assert [call.args[:2] for call in request.call_args_list] == [
        ("GET", "/api/health"),
        ("GET", "/api/system/home-on-init"),
        ("GET", "/api/robot/debug/motors"),
    ]


@pytest.mark.parametrize(
    "responses",
    [
        [{"status": "ok", "mode": "production", "robot_status": "ok", "robot_phase": "ready"}],
        [
            {"status": "ok", "mode": "development", "robot_status": "ok", "robot_phase": "ready"},
            {"home_on_init": True},
        ],
        [
            {"status": "ok", "mode": "development", "robot_status": "ok", "robot_phase": "ready"},
            {"home_on_init": False},
            {"tool": [{"name": "y_axis", "motor_id": 3}]},
        ],
    ],
)
def test_api_preflight_fails_closed(responses, monkeypatch) -> None:
    module = load_module()
    iterator = iter(responses)
    monkeypatch.setattr(module, "request_json", Mock(side_effect=lambda *_a, **_k: next(iterator)))
    monkeypatch.setattr(module, "request_robot_status", Mock(return_value=safe_status()))
    with pytest.raises(RuntimeError):
        module.api_preflight("token")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda status: status["robot_status"]["safety"].update({"bypassed": True}),
        lambda status: status["robot_status"]["safety"]["component_bypasses"].update(
            {"cabinet_doors": True}
        ),
        lambda status: status["robot_status"]["safety"].update(
            {"motion_blocked": True, "awaiting_ack": True}
        ),
        lambda status: status["robot_status"]["gantry"]["y_axis"].update(
            {"position": 50.001}
        ),
        lambda status: status.update({"queue_length": 1}),
    ],
)
def test_api_preflight_rejects_unsafe_live_robot_status(monkeypatch, mutate) -> None:
    module = load_module()
    responses = iter(
        [
            {"status": "ok", "mode": "development", "robot_status": "ok", "robot_phase": "ready"},
            {"home_on_init": False},
            {"tool": [{"name": "y_axis", "motor_id": 2}]},
        ]
    )
    status = safe_status()
    mutate(status)
    monkeypatch.setattr(module, "request_json", Mock(side_effect=lambda *_a, **_k: next(responses)))
    monkeypatch.setattr(module, "request_robot_status", Mock(return_value=status))
    with pytest.raises(RuntimeError):
        module.api_preflight("token")


def test_api_preflight_allows_only_explicit_absent_cabinet_door_bypass(
    monkeypatch,
) -> None:
    module = load_module()
    responses = iter(
        [
            {"status": "ok", "mode": "development", "robot_status": "ok", "robot_phase": "ready"},
            {"home_on_init": False},
            {"tool": [{"name": "y_axis", "motor_id": 2}]},
        ]
    )
    status = safe_status()
    status["robot_status"]["safety"]["component_bypasses"] = {
        "cabinet_doors": True,
        "light_curtain": False,
    }
    monkeypatch.setattr(module, "request_json", Mock(side_effect=lambda *_a, **_k: next(responses)))
    monkeypatch.setattr(module, "request_robot_status", Mock(return_value=status))
    module.api_preflight("token", allow_cabinet_doors_bypass=True)


@pytest.mark.parametrize(
    "component_bypasses",
    [
        {"cabinet_doors": False, "light_curtain": False},
        {"cabinet_doors": True, "light_curtain": True},
        {"cabinet_doors": True, "light_curtain": 0},
    ],
)
def test_explicit_cabinet_bypass_mode_fails_closed_for_any_other_state(
    monkeypatch, component_bypasses
) -> None:
    module = load_module()
    responses = iter(
        [
            {"status": "ok", "mode": "development", "robot_status": "ok", "robot_phase": "ready"},
            {"home_on_init": False},
            {"tool": [{"name": "y_axis", "motor_id": 2}]},
        ]
    )
    status = safe_status()
    status["robot_status"]["safety"]["component_bypasses"] = component_bypasses
    monkeypatch.setattr(module, "request_json", Mock(side_effect=lambda *_a, **_k: next(responses)))
    monkeypatch.setattr(module, "request_robot_status", Mock(return_value=status))
    with pytest.raises(RuntimeError, match="explicitly allowed"):
        module.api_preflight("token", allow_cabinet_doors_bypass=True)


def test_enable_absent_cabinet_doors_bypass_has_strict_gate(monkeypatch) -> None:
    module = load_module()
    status = safe_status()
    safety = status["robot_status"]["safety"]
    safety.update(
        {
            "active": True,
            "motion_blocked": True,
            "awaiting_ack": True,
            "active_sources": ["cabinet_doors"],
            "sources": {
                "cabinet_doors": {"active": True, "open": True, "bypassed": False},
                "light_curtain": {"active": False, "bypassed": False},
            },
        }
    )
    response = {
        "active": False,
        "motion_blocked": False,
        "awaiting_ack": False,
        "active_sources": [],
        "bypassed": False,
        "component_bypasses": {"cabinet_doors": True, "light_curtain": False},
    }
    request = Mock(
        side_effect=[
            {"home_on_init": False},
            {"tool": [{"name": "y_axis", "motor_id": 2}]},
            response,
        ]
    )
    monkeypatch.setattr(module, "request_robot_status", Mock(return_value=status))
    monkeypatch.setattr(module, "request_json", request)
    module.enable_absent_cabinet_doors_bypass("token")
    assert request.call_args_list[-1].args == (
        "POST",
        "/api/robot/safety/override/component",
    )
    assert request.call_args_list[-1].kwargs == {
        "token": "token",
        "payload": {"component": "cabinet_doors", "bypass": True},
    }


def test_enable_absent_cabinet_doors_bypass_rejects_curtain_activity(
    monkeypatch,
) -> None:
    module = load_module()
    status = safe_status()
    safety = status["robot_status"]["safety"]
    safety.update(
        {
            "active": True,
            "motion_blocked": True,
            "active_sources": ["cabinet_doors"],
            "sources": {
                "cabinet_doors": {"active": True, "open": True, "bypassed": False},
                "light_curtain": {"active": True, "bypassed": False},
            },
        }
    )
    request = Mock(
        side_effect=[
            {"home_on_init": False},
            {"tool": [{"name": "y_axis", "motor_id": 2}]},
        ]
    )
    monkeypatch.setattr(module, "request_robot_status", Mock(return_value=status))
    monkeypatch.setattr(module, "request_json", request)
    with pytest.raises(RuntimeError, match="doorless-machine condition"):
        module.enable_absent_cabinet_doors_bypass("token")
    assert request.call_count == 2


def test_home_repeats_preflight_then_calls_only_y_home(monkeypatch, tmp_path: Path) -> None:
    module = load_module()
    trace = tmp_path / "trace.jsonl"
    completed_trace(module, trace)
    args = SimpleNamespace(
        command="home-after-5s",
        email="developer@opshelf.com",
        position_trace=trace,
        confirm=module.HOME_CONFIRMATION,
        enable_absent_cabinet_doors_bypass=False,
    )
    authenticate = Mock(return_value="token")
    preflight = Mock(return_value={})
    wait_for_ready = Mock()
    countdown = Mock()
    request = Mock(return_value={"success": True, "motor": "y_axis"})
    monkeypatch.setattr(module, "authenticate", authenticate)
    monkeypatch.setattr(module, "wait_for_ready_api", wait_for_ready)
    monkeypatch.setattr(module, "api_preflight", preflight)
    monkeypatch.setattr(module, "countdown", countdown)
    monkeypatch.setattr(module, "request_json", request)

    assert module.run(args) == 0
    assert preflight.call_count == 2
    assert all(
        call.kwargs == {"allow_cabinet_doors_bypass": False}
        for call in preflight.call_args_list
    )
    wait_for_ready.assert_called_once()
    countdown.assert_called_once_with("home-after-5s", 5.0)
    request.assert_called_once_with(
        "POST",
        "/api/robot/debug/motors/y_axis/home",
        token="token",
        timeout=module.HOME_TIMEOUT_SECONDS,
    )


def test_check_is_read_only(monkeypatch) -> None:
    module = load_module()
    args = SimpleNamespace(command="check", email="developer@opshelf.com")
    monkeypatch.setattr(module, "authenticate", Mock(return_value="token"))
    monkeypatch.setattr(module, "api_preflight", Mock(return_value={}))
    request = Mock()
    monkeypatch.setattr(module, "request_json", request)
    assert module.run(args) == 0
    request.assert_not_called()


def test_wait_for_ready_api_polls_until_exact_ready(monkeypatch) -> None:
    module = load_module()
    responses = iter(
        [
            RuntimeError("HTTP 503"),
            {"status": "ok", "mode": "development", "robot_status": "initializing"},
            {
                "status": "ok",
                "mode": "development",
                "robot_status": "ok",
                "robot_phase": "ready",
            },
        ]
    )

    def request(*_args, **_kwargs):
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(module, "request_json", Mock(side_effect=request))
    monkeypatch.setattr(module.time, "sleep", Mock())
    module.wait_for_ready_api()
    assert module.request_json.call_count == 3
