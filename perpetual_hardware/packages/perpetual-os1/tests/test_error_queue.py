#!/usr/bin/env python3
"""Test script to verify error queue functionality in WorkerStatus"""

import asyncio
import json
from datetime import datetime
from perpetual_os1.robot_worker import WorkerStatus, WorkerError


def test_worker_error_serialization():
	"""Test that WorkerError can be serialized and deserialized"""
	error = WorkerError(
		error_type="TestError",
		message="This is a test error",
		context={"command": "test_command", "value": 42},
		stack_trace="Traceback..."
	)
	
	# Test JSON serialization
	json_str = error.model_dump_json()
	parsed = WorkerError.model_validate_json(json_str)
	
	assert parsed.error_type == "TestError"
	assert parsed.message == "This is a test error"
	assert parsed.context["command"] == "test_command"
	assert parsed.stack_trace == "Traceback..."
	print("✓ WorkerError serialization test passed")


def test_worker_status_with_errors():
	"""Test that WorkerStatus includes error queue"""
	errors = [
		WorkerError(
			error_type="TimeoutError",
			message="Operation timed out",
			context={"command": "move_to"}
		),
		WorkerError(
			error_type="HardwareError",
			message="Motor stalled",
			context={"motor": "X", "position": 100}
		)
	]
	
	status = WorkerStatus(
		robot_status=None,
		running=True,
		current_command=None,
		error_queue=errors
	)
	
	# Test JSON serialization
	json_str = status.model_dump_json()
	parsed = WorkerStatus.model_validate_json(json_str)
	
	assert len(parsed.error_queue) == 2
	assert parsed.error_queue[0].error_type == "TimeoutError"
	assert parsed.error_queue[1].error_type == "HardwareError"
	print("✓ WorkerStatus with error queue test passed")


def test_error_queue_limit():
	"""Test that error queue maintains max size of 10"""
	from perpetual_os1.robot_worker import RobotWorker
	from perpetual_os1.robot import Robot
	from unittest.mock import Mock
	
	# Create a mock robot
	mock_robot = Mock(spec=Robot)
	
	# Create worker (will fail due to singleton check, but we can test the method)
	worker = object.__new__(RobotWorker)
	worker._error_queue = []
	
	# Add 15 errors
	for i in range(15):
		error = WorkerError(
			error_type="TestError",
			message=f"Error {i}"
		)
		worker._add_error(error)
	
	# Should only have last 10 errors
	assert len(worker._error_queue) == 10
	assert worker._error_queue[0].message == "Error 5"
	assert worker._error_queue[9].message == "Error 14"
	print("✓ Error queue size limit test passed")


def main():
	"""Run all tests"""
	print("Testing SSE Error Reporting Implementation...")
	print("-" * 50)
	
	test_worker_error_serialization()
	test_worker_status_with_errors()
	test_error_queue_limit()
	
	print("-" * 50)
	print("All tests passed! ✅")


if __name__ == "__main__":
	main()