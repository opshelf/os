from __future__ import annotations


from openshelf_os_server import main


# The value anyio puts on a CancelledError it delivered on behalf of BaseHTTPMiddleware's
# call_next task, which is how a streaming request is cancelled when its response finishes.


def test_before_send_sentry_event_redacts_headers() -> None:
	event = {
		"request": {
			"headers": {
				"authorization": "secret",
				"x-request-id": "request-id",
			}
		}
	}

	assert main._before_send_sentry_event(event, {}) == event
	assert event["request"]["headers"]["authorization"] == "[Filtered]"
	assert event["request"]["headers"]["x-request-id"] == "request-id"


def test_before_send_sentry_event_drops_injected_motor_fault() -> None:
	"""The debug fault-injection endpoint's HardwareFault event must not reach Sentry."""
	# What the worker actually sends: a stackless capture_message whose fault text lives in
	# the hardware_fault/worker_error contexts (no exception frames).
	event = {
		"transaction": "openshelf_os_server.routers.robot.simulate_motor_crash",
		"message": "HardwareFault: Simulated motor fault on z_axis",
		"contexts": {
			"worker_error": {
				"simulated_fault": True,
				"message": "Simulated motor fault on z_axis",
			},
			"hardware_fault": {"message": "Simulated motor fault on z_axis"},
		},
	}

	assert main._before_send_sentry_event(event, {}) is None

	# The same fault reported with exception frames attached is dropped as well.
	event_with_exception = {
		"contexts": {"worker_error": {"simulated_fault": True}},
		"exception": {
			"values": [{"type": "RuntimeError", "value": "Simulated motor fault on z_axis"}]
		},
	}

	assert main._before_send_sentry_event(event_with_exception, {}) is None


def test_before_send_sentry_event_keeps_real_fault_while_injected_fault_is_latched() -> None:
	"""A real failure reported while the injected-fault marker is present stays reportable."""
	event = {
		"contexts": {
			"worker_error": {"simulated_fault": True},
			"hardware_fault": {"message": "Fault detected on motor 5"},
		},
		"exception": {"values": [{"type": "MotorError", "value": "Fault detected on motor 5"}]},
	}

	assert main._before_send_sentry_event(event, {}) == event

	# A simulated-looking message without the worker's injected-fault marker is kept too.
	unmarked = {
		"contexts": {"hardware_fault": {"message": "Simulated motor fault on z_axis"}},
		"exception": {
			"values": [{"type": "RuntimeError", "value": "Simulated motor fault on z_axis"}]
		},
	}

	assert main._before_send_sentry_event(unmarked, {}) == unmarked
