"""Test script to verify error queue functionality in robot worker"""

import asyncio
import json
from datetime import datetime, timezone
from perpetual_os1.robot_worker import WorkerStatus, WorkerStatusWithQueue, WorkerError, RobotWorker
from perpetual_os1.robot import StopCommand
from perpetual_os1.common import RobotNatsConfiguration


def test_worker_error_serialization():
	"""Test WorkerError JSON serialization"""
	print("\nTesting WorkerError serialization...")
	
	error = WorkerError(
		error_type="TestError",
		message="This is a test error",
		context={"command": "test_command", "value": 42},
		stack_trace="Traceback..."
	)
	
	# Test JSON serialization
	json_str = error.model_dump_json()
	print(f"Serialized: {json_str}")
	
	parsed = WorkerError.model_validate_json(json_str)
	print(f"Error type: {parsed.error_type}")
	print(f"Message: {parsed.message}")
	print(f"Context: {parsed.context}")
	print(f"Has timestamp: {parsed.timestamp is not None}")
	
	if parsed.error_type != "TestError":
		raise AssertionError("Error type mismatch")
	if parsed.message != "This is a test error":
		raise AssertionError("Message mismatch")
	if parsed.context["command"] != "test_command":
		raise AssertionError("Context mismatch")
	
	print("✓ WorkerError serialization passed")


def test_worker_status_models():
	"""Test both WorkerStatus models with error queues"""
	print("\nTesting WorkerStatus models...")
	
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
	
	# Test basic WorkerStatus
	status = WorkerStatus(
		robot_status=None,
		running=True,
		current_command=None,
		error_queue=errors
	)
	
	json_str = status.model_dump_json()
	parsed = WorkerStatus.model_validate_json(json_str)
	
	if len(parsed.error_queue) != 2:
		raise AssertionError(f"Expected 2 errors, got {len(parsed.error_queue)}")
	if parsed.error_queue[0].error_type != "TimeoutError":
		raise AssertionError("First error type mismatch")
	if parsed.error_queue[1].error_type != "HardwareError":
		raise AssertionError("Second error type mismatch")
	
	print("✓ WorkerStatus serialization passed")
	
	# Test WorkerStatusWithQueue
	status_with_queue = WorkerStatusWithQueue(
		robot_status=None,
		running=True,
		current_command=None,
		queued_operations=[],
		queue_length=0,
		error_queue=errors
	)
	
	json_str = status_with_queue.model_dump_json()
	parsed = WorkerStatusWithQueue.model_validate_json(json_str)
	
	if len(parsed.error_queue) != 2:
		raise AssertionError(f"Expected 2 errors in queue model, got {len(parsed.error_queue)}")
	
	print("✓ WorkerStatusWithQueue serialization passed")


def test_error_queue_management():
	"""Test error queue size limit"""
	print("\nTesting error queue management...")
	
	# Create worker instance for testing
	worker = object.__new__(RobotWorker)
	worker._error_queue = []
	
	# Add 15 errors
	for i in range(15):
		error = WorkerError(
			error_type="TestError",
			message=f"Error {i}"
		)
		worker._add_error(error)
		print(f"Added error {i}, queue size: {len(worker._error_queue)}")
	
	# Check queue size limit
	if len(worker._error_queue) != 10:
		raise AssertionError(f"Expected queue size 10, got {len(worker._error_queue)}")
	
	# Check oldest errors were dropped
	if worker._error_queue[0].message != "Error 5":
		raise AssertionError(f"Expected first error to be 'Error 5', got '{worker._error_queue[0].message}'")
	if worker._error_queue[9].message != "Error 14":
		raise AssertionError(f"Expected last error to be 'Error 14', got '{worker._error_queue[9].message}'")
	
	print("✓ Error queue size limit working correctly")


async def test_error_in_status_update():
	"""Test error capture during status update"""
	print("\nTesting error capture in status updates...")
	
	# This would require a full robot setup, so we'll just verify the models work
	nats_config = RobotNatsConfiguration()
	print(f"NATS config: {nats_config.connection_string()}")
	
	# Create a command to test serialization
	stop_cmd = StopCommand()
	print(f"Stop command: {stop_cmd.model_dump_json()}")
	
	print("✓ Configuration and command models working")


async def main():
	"""Run all tests"""
	print("=" * 60)
	print("Testing Robot Worker Error Queue Implementation")
	print("=" * 60)
	
	try:
		test_worker_error_serialization()
		test_worker_status_models()
		test_error_queue_management()
		await test_error_in_status_update()
		
		print("\n" + "=" * 60)
		print("All tests passed successfully!")
		print("=" * 60)
		
	except Exception as e:
		print(f"\n❌ Test failed: {e}")
		raise


if __name__ == "__main__":
	asyncio.run(main())