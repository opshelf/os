import asyncio
import importlib.util
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


def load_brake_drive_incident_module():
	repo_root = Path(__file__).resolve().parents[4]
	script_path = repo_root / "scripts" / "brake_drive_incident.py"
	spec = importlib.util.spec_from_file_location("brake_drive_incident_test_module", script_path)
	module = importlib.util.module_from_spec(spec)
	assert spec.loader is not None
	spec.loader.exec_module(module)
	return module


def test_refuse_if_api_is_live_rejects_before_hardware_access(monkeypatch: pytest.MonkeyPatch) -> None:
	module = load_brake_drive_incident_module()
	connection = Mock()
	create_connection = Mock(return_value=connection)
	monkeypatch.setattr(module.socket, "create_connection", create_connection)

	with pytest.raises(RuntimeError, match="normal API appears to be listening"):
		module.refuse_if_api_is_live("127.0.0.1", 8000)

	create_connection.assert_called_once_with(
		("127.0.0.1", 8000),
		timeout=module.API_LIVE_CHECK_TIMEOUT_SECONDS,
	)
	connection.close.assert_called_once()


def test_parser_does_not_offer_bypassable_exclusivity_guards() -> None:
	module = load_brake_drive_incident_module()
	parser = module.build_parser()

	for option in ("--api-host", "--api-port", "--ownership-lock"):
		assert option not in parser._option_string_actions
	assert module.NORMAL_API_HOST == "127.0.0.1"
	assert module.NORMAL_API_PORT == 8000
	assert module.OWNERSHIP_LOCK_PATH == (
		module.REPO_ROOT / "openshelf_hardware/data/.brake-drive-incident.lock"
	)


@pytest.mark.parametrize(
	"arguments",
	[
		["matrix"],
		[
			"same-position-trigger",
			"--expect-output-function",
			"0xA1",
			"--confirm",
			"SUPERVISED-ISOLATED-Y-TEST",
		],
		["application-noop", "--config", "test-config.json"],
		[
			"persist-profile",
			"--confirm",
			"PERSIST-CONFIRMED-CS-M234120B-MS30-PROFILE",
		],
	],
)
def test_parser_requires_explicit_cs2b_register_map(arguments: list[str]) -> None:
	module = load_brake_drive_incident_module()
	parser = module.build_parser()

	with pytest.raises(SystemExit):
		parser.parse_args(arguments)


@pytest.mark.parametrize("output_function", [0x9A, 0x24, 0xA4])
def test_same_position_trigger_refuses_non_legacy_output_function(output_function: int) -> None:
	module = load_brake_drive_incident_module()
	args = module.build_parser().parse_args(
		[
			"same-position-trigger",
			"--register-map",
			module.SUPPORTED_REGISTER_MAP,
			"--expect-output-function",
			hex(output_function),
			"--confirm",
			module.WRITE_CONFIRMATION,
		]
	)

	with pytest.raises(ValueError, match="only reproduces the legacy path-completed"):
		module.validate_args(args)


def test_same_position_trigger_accepts_legacy_output_function() -> None:
	module = load_brake_drive_incident_module()
	args = module.build_parser().parse_args(
		[
			"same-position-trigger",
			"--register-map",
			module.SUPPORTED_REGISTER_MAP,
			"--expect-output-function",
			"0xA1",
			"--confirm",
			module.WRITE_CONFIRMATION,
		]
	)

	module.validate_args(args)


@pytest.mark.parametrize(
	("option", "value", "message"),
	[
		("--unit", "3", "restricted to Y unit 2"),
		("--cycles", "2", "exactly one cycle"),
	],
)
def test_same_position_trigger_is_restricted_to_one_y_cycle(
	option: str,
	value: str,
	message: str,
) -> None:
	module = load_brake_drive_incident_module()
	args = module.build_parser().parse_args(
		[
			"same-position-trigger",
			"--register-map",
			module.SUPPORTED_REGISTER_MAP,
			"--expect-output-function",
			"0xA1",
			"--confirm",
			module.WRITE_CONFIRMATION,
			option,
			value,
		]
	)

	with pytest.raises(ValueError, match=message):
		module.validate_args(args)


def test_application_noop_is_restricted_to_y_axis() -> None:
	module = load_brake_drive_incident_module()
	args = module.build_parser().parse_args(
		[
			"application-noop",
			"--register-map",
			module.SUPPORTED_REGISTER_MAP,
			"--config",
			"test-config.json",
			"--motor-name",
			"x_axis",
		]
	)

	with pytest.raises(ValueError, match="restricted to motor-name y_axis"):
		module.validate_args(args)


@pytest.mark.asyncio
@pytest.mark.parametrize(
	("alarm", "position", "message"),
	[
		(1, (1, 2), "alarm became nonzero"),
		(0, (1, 3), "encoder position changed"),
	],
)
async def test_trigger_sampling_fails_on_alarm_or_encoder_change(
	monkeypatch: pytest.MonkeyPatch,
	alarm: int,
	position: tuple[int, int],
	message: str,
) -> None:
	module = load_brake_drive_incident_module()
	values = {
		"output_state": (0,),
		"status": (0,),
		"alarm": (alarm,),
		"position_block": position,
	}
	monkeypatch.setattr(
		module,
		"read_register",
		AsyncMock(side_effect=lambda _controller, _unit, name: values[name]),
	)
	args = SimpleNamespace(
		samples=1,
		unit=module.Y_AXIS_UNIT,
		sample_interval_ms=0,
	)

	with pytest.raises(RuntimeError, match=message):
		await module.sample_trigger_state(args, SimpleNamespace(), 0, (1, 2))


def test_persist_profile_requires_exact_confirmation() -> None:
	module = load_brake_drive_incident_module()
	args = module.build_parser().parse_args(
		[
			"persist-profile",
			"--register-map",
			module.SUPPORTED_REGISTER_MAP,
			"--confirm",
			"wrong",
		]
	)

	with pytest.raises(ValueError, match="persistence confirmation mismatch"):
		module.validate_args(args)


def profile_read_side_effect(module, *, overrides=None, save_status=None):
	values = {
		module.REGISTERS[name][0]: (expected,)
		for name, expected in module.PERSISTED_Y_PROFILE.items()
	}
	values[module.REGISTERS["status"][0]] = (0,)
	values[module.REGISTERS["alarm"][0]] = (0,)
	values[module.REGISTERS["position_block"][0]] = (0x0001, 0x0002)
	if overrides:
		values.update(overrides)

	async def read(_unit, address, *, count):
		if address == module.SAVE_STATUS_REGISTER:
			return (module.SAVE_SUCCEEDED if save_status is None else save_status,)
		value = values[address]
		assert len(value) == count
		return value

	return read


@pytest.mark.asyncio
async def test_persist_profile_saves_only_after_exact_stationary_healthy_preflight() -> None:
	module = load_brake_drive_incident_module()
	controller = SimpleNamespace(
		read_holding_register_block=AsyncMock(side_effect=profile_read_side_effect(module)),
		write_single_register=AsyncMock(),
	)
	args = SimpleNamespace(save_timeout_s=1.0)

	assert await module.run_persist_profile(args, controller) == 0

	controller.write_single_register.assert_awaited_once_with(
		module.Y_AXIS_UNIT,
		module.SAVE_CONTROL_REGISTER,
		module.SAVE_PARAMETERS_COMMAND,
	)


@pytest.mark.asyncio
async def test_persist_profile_refuses_profile_mismatch_without_writing() -> None:
	module = load_brake_drive_incident_module()
	control_mode_address = module.REGISTERS["control_mode"][0]
	controller = SimpleNamespace(
		read_holding_register_block=AsyncMock(
			side_effect=profile_read_side_effect(
				module,
				overrides={control_mode_address: (0,)},
			)
		),
		write_single_register=AsyncMock(),
	)

	with pytest.raises(RuntimeError, match="mismatched registers"):
		await module.run_persist_profile(SimpleNamespace(save_timeout_s=1.0), controller)

	controller.write_single_register.assert_not_awaited()


@pytest.mark.asyncio
async def test_persist_profile_refuses_running_or_alarm_state_without_writing() -> None:
	module = load_brake_drive_incident_module()
	status_address = module.REGISTERS["status"][0]
	controller = SimpleNamespace(
		read_holding_register_block=AsyncMock(
			side_effect=profile_read_side_effect(
				module,
				overrides={status_address: (1 << module.MOTION_RUNNING_BIT,)},
			)
		),
		write_single_register=AsyncMock(),
	)

	with pytest.raises(RuntimeError, match="status is running"):
		await module.run_persist_profile(SimpleNamespace(save_timeout_s=1.0), controller)

	controller.write_single_register.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_main_uses_canonical_guards_before_serial_connect(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	module = load_brake_drive_incident_module()
	args = module.build_parser().parse_args(
		[
			"matrix",
			"--register-map",
			module.SUPPORTED_REGISTER_MAP,
			"--iterations",
			"1",
		]
	)
	events: list[str] = []
	controller = Mock()

	async def connect() -> None:
		events.append("connect")

	async def close() -> None:
		events.append("close")

	controller.connect = AsyncMock(side_effect=connect)
	controller.close = AsyncMock(side_effect=close)

	@contextmanager
	def lock(lock_path: Path):
		assert lock_path == module.OWNERSHIP_LOCK_PATH
		events.append("lock")
		yield

	def api_guard(host: str, port: int) -> None:
		assert (host, port) == (module.NORMAL_API_HOST, module.NORMAL_API_PORT)
		events.append("api_guard")

	def make_controller(_args):
		events.append("make_controller")
		return controller

	async def run_matrix(_args, received_controller) -> int:
		assert received_controller is controller
		events.append("run_matrix")
		return 0

	monkeypatch.setattr(module, "acquire_ownership_lock", lock)
	monkeypatch.setattr(module, "refuse_if_api_is_live", api_guard)
	monkeypatch.setattr(module, "make_controller", make_controller)
	monkeypatch.setattr(module, "run_matrix", run_matrix)

	assert await module.async_main(args) == 0
	assert events == ["lock", "api_guard", "make_controller", "connect", "run_matrix", "close"]
	controller.connect.assert_awaited_once()
	controller.close.assert_awaited_once()


def test_acquire_ownership_lock_rejects_contention(tmp_path: Path) -> None:
	module = load_brake_drive_incident_module()
	lock_path = tmp_path / ".brake-drive-incident.lock"

	with module.acquire_ownership_lock(lock_path):
		with pytest.raises(RuntimeError, match="another brake-drive incident harness already holds"):
			with module.acquire_ownership_lock(lock_path):
				pass


def application_noop_config(motor_params: SimpleNamespace) -> SimpleNamespace:
	return SimpleNamespace(robot=SimpleNamespace(tool=SimpleNamespace(motors=[motor_params])))


@pytest.mark.asyncio
async def test_application_noop_rejects_fallback_position_before_movement(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	module = load_brake_drive_incident_module()
	motor_params = SimpleNamespace(
		motor_name="y_axis", enabled=True, motor_id=2, lower_lim=0, lim=100
	)
	controller = SimpleNamespace(write_single_register=AsyncMock())

	class FakeMotor:
		instance: "FakeMotor"

		def __init__(self, _controller, _motor_params) -> None:
			self._cur_pos = None
			self._last_raw_position = None
			self.move_to_position_no_wait = AsyncMock(return_value=False)
			FakeMotor.instance = self

		async def get_brake_output_diagnostics(self):
			return {"register_function": 0x24}

		async def get_position(self) -> float:
			return 0.0

	monkeypatch.setattr(module, "load_config_file", lambda _path: application_noop_config(motor_params))
	monkeypatch.setattr(module, "Motor", FakeMotor)

	args = SimpleNamespace(config=Path("test-config.json"), motor_name="y_axis")
	with pytest.raises(RuntimeError, match="fresh raw-backed"):
		await module.run_application_noop(args, controller)

	FakeMotor.instance.move_to_position_no_wait.assert_not_awaited()
	controller.write_single_register.assert_not_awaited()


@pytest.mark.asyncio
async def test_application_noop_rejects_out_of_bounds_position_before_movement(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	module = load_brake_drive_incident_module()
	motor_params = SimpleNamespace(
		motor_name="y_axis", enabled=True, motor_id=2, lower_lim=10, lim=100
	)
	controller = SimpleNamespace(write_single_register=AsyncMock())

	class FakeMotor:
		instance: "FakeMotor"

		def __init__(self, _controller, _motor_params) -> None:
			self._cur_pos = 101.0
			self._last_raw_position = 123
			self.move_to_position_no_wait = AsyncMock(return_value=False)
			FakeMotor.instance = self

		async def get_brake_output_diagnostics(self):
			return {"register_function": 0x24}

		async def get_position(self) -> float:
			return 101.0

	monkeypatch.setattr(module, "load_config_file", lambda _path: application_noop_config(motor_params))
	monkeypatch.setattr(module, "Motor", FakeMotor)

	args = SimpleNamespace(config=Path("test-config.json"), motor_name="y_axis")
	with pytest.raises(RuntimeError, match="outside configured motor limits"):
		await module.run_application_noop(args, controller)

	FakeMotor.instance.move_to_position_no_wait.assert_not_awaited()
	controller.write_single_register.assert_not_awaited()


@pytest.mark.asyncio
async def test_application_noop_stops_before_reading_after_diagnostics_when_path_starts(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	module = load_brake_drive_incident_module()
	motor_params = SimpleNamespace(
		motor_name="y_axis", enabled=True, motor_id=2, lower_lim=0, lim=100
	)

	class FakeMotor:
		instance: "FakeMotor"

		def __init__(self, _controller, _motor_params) -> None:
			self._cur_pos = 50.0
			self._last_raw_position = 123
			self.move_to_position_no_wait = AsyncMock(return_value=True)
			self._stop_and_clear_motion = AsyncMock()
			self.get_brake_output_diagnostics = AsyncMock(
				return_value={"register_function": 0x24}
			)
			self.get_position = AsyncMock(return_value=50.0)
			FakeMotor.instance = self

	monkeypatch.setattr(module, "load_config_file", lambda _path: application_noop_config(motor_params))
	monkeypatch.setattr(module, "Motor", FakeMotor)

	args = SimpleNamespace(config=Path("test-config.json"), motor_name="y_axis")
	with pytest.raises(RuntimeError, match="unexpectedly emitted a path command"):
		await module.run_application_noop(args, SimpleNamespace())

	FakeMotor.instance._stop_and_clear_motion.assert_awaited_once()
	assert FakeMotor.instance.get_brake_output_diagnostics.await_count == 1
	assert FakeMotor.instance.get_position.await_count == 1


@pytest.mark.asyncio
async def test_application_noop_reports_stop_failure_when_path_starts(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	module = load_brake_drive_incident_module()
	motor_params = SimpleNamespace(
		motor_name="y_axis", enabled=True, motor_id=2, lower_lim=0, lim=100
	)

	class FakeMotor:
		instance: "FakeMotor"

		def __init__(self, _controller, _motor_params) -> None:
			self._cur_pos = 50.0
			self._last_raw_position = 123
			self.move_to_position_no_wait = AsyncMock(return_value=True)
			self._stop_and_clear_motion = AsyncMock(side_effect=OSError("stop failed"))
			self.get_brake_output_diagnostics = AsyncMock(
				return_value={"register_function": 0x24}
			)
			self.get_position = AsyncMock(return_value=50.0)
			FakeMotor.instance = self

	monkeypatch.setattr(module, "load_config_file", lambda _path: application_noop_config(motor_params))
	monkeypatch.setattr(module, "Motor", FakeMotor)

	args = SimpleNamespace(config=Path("test-config.json"), motor_name="y_axis")
	with pytest.raises(
		RuntimeError,
		match="unexpectedly emitted a path command; emergency stop-and-clear failed: stop failed",
	):
		await module.run_application_noop(args, SimpleNamespace())

	FakeMotor.instance._stop_and_clear_motion.assert_awaited_once()
	assert FakeMotor.instance.get_brake_output_diagnostics.await_count == 1


@pytest.mark.asyncio
async def test_application_noop_finishes_cleanup_before_propagating_cancellation_during_path_start(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	module = load_brake_drive_incident_module()
	motor_params = SimpleNamespace(
		motor_name="y_axis", enabled=True, motor_id=2, lower_lim=0, lim=100
	)
	cleanup_started = asyncio.Event()
	release_cleanup = asyncio.Event()

	class FakeMotor:
		instance: "FakeMotor"

		def __init__(self, _controller, _motor_params) -> None:
			self._cur_pos = 50.0
			self._last_raw_position = 123
			self.cleanup_finished = False
			FakeMotor.instance = self

		async def move_to_position_no_wait(self, _position: float) -> bool:
			return True

		async def _stop_and_clear_motion(self) -> None:
			cleanup_started.set()
			await release_cleanup.wait()
			self.cleanup_finished = True

		async def get_brake_output_diagnostics(self):
			return {"register_function": 0x24}

		async def get_position(self) -> float:
			return 50.0

	monkeypatch.setattr(module, "load_config_file", lambda _path: application_noop_config(motor_params))
	monkeypatch.setattr(module, "Motor", FakeMotor)

	args = SimpleNamespace(config=Path("test-config.json"), motor_name="y_axis")
	task = asyncio.create_task(module.run_application_noop(args, SimpleNamespace()))
	await cleanup_started.wait()
	task.cancel()
	await asyncio.sleep(0)
	assert task.done() is False
	assert FakeMotor.instance.cleanup_finished is False

	release_cleanup.set()
	with pytest.raises(asyncio.CancelledError):
		await task

	assert FakeMotor.instance.cleanup_finished is True


@pytest.mark.asyncio
async def test_application_noop_stops_before_propagating_movement_cancellation(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	module = load_brake_drive_incident_module()
	motor_params = SimpleNamespace(
		motor_name="y_axis", enabled=True, motor_id=2, lower_lim=0, lim=100
	)

	class FakeMotor:
		instance: "FakeMotor"

		def __init__(self, _controller, _motor_params) -> None:
			self._cur_pos = 50.0
			self._last_raw_position = 123
			self.triggered = False
			self._stop_and_clear_motion = AsyncMock()
			FakeMotor.instance = self

		async def move_to_position_no_wait(self, _position: float) -> bool:
			self.triggered = True
			raise asyncio.CancelledError()

		async def get_brake_output_diagnostics(self):
			return {"register_function": 0x24}

		async def get_position(self) -> float:
			return 50.0

	monkeypatch.setattr(module, "load_config_file", lambda _path: application_noop_config(motor_params))
	monkeypatch.setattr(module, "Motor", FakeMotor)

	args = SimpleNamespace(config=Path("test-config.json"), motor_name="y_axis")
	with pytest.raises(asyncio.CancelledError):
		await module.run_application_noop(args, SimpleNamespace())

	assert FakeMotor.instance.triggered is True
	FakeMotor.instance._stop_and_clear_motion.assert_awaited_once()


@pytest.mark.asyncio
async def test_application_noop_preserves_movement_cancellation_when_cleanup_fails(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	module = load_brake_drive_incident_module()
	motor_params = SimpleNamespace(
		motor_name="y_axis", enabled=True, motor_id=2, lower_lim=0, lim=100
	)

	class FakeMotor:
		instance: "FakeMotor"

		def __init__(self, _controller, _motor_params) -> None:
			self._cur_pos = 50.0
			self._last_raw_position = 123
			self.move_to_position_no_wait = AsyncMock(side_effect=asyncio.CancelledError())
			self._stop_and_clear_motion = AsyncMock(side_effect=OSError("stop failed"))
			FakeMotor.instance = self

		async def get_brake_output_diagnostics(self):
			return {"register_function": 0x24}

		async def get_position(self) -> float:
			return 50.0

	monkeypatch.setattr(module, "load_config_file", lambda _path: application_noop_config(motor_params))
	monkeypatch.setattr(module, "Motor", FakeMotor)

	args = SimpleNamespace(config=Path("test-config.json"), motor_name="y_axis")
	with pytest.raises(asyncio.CancelledError) as exc_info:
		await module.run_application_noop(args, SimpleNamespace())

	assert isinstance(exc_info.value.cleanup_error, OSError)
	assert "emergency stop-and-clear" in exc_info.value.__notes__[0]
	FakeMotor.instance._stop_and_clear_motion.assert_awaited_once()
