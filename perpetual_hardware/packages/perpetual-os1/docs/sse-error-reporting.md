# SSE Error Reporting

## Overview

The robot worker now includes an error queue in its status updates, allowing clients to receive real-time error information through Server-Sent Events (SSE).

## Error Model

### WorkerError
```python
class WorkerError(BaseModel):
    timestamp: datetime  # UTC timestamp when error occurred
    error_type: str      # Error category (e.g., "TimeoutError", "HardwareError")
    message: str         # Human-readable error description
    context: Optional[dict] = None  # Additional context (command, hardware state)
    stack_trace: Optional[str] = None  # Python stack trace for debugging
```

### WorkerStatus with Error Queue
```python
class WorkerStatus(BaseModel):
    robot_status: Optional[RobotStatus]
    running: bool
    current_command: Optional[RobotCommands]
    error_queue: List[WorkerError]  # Up to 10 most recent errors
```

## Error Types

- **CommandExecutionError**: Command failed during execution
- **TimeoutError**: Operation timed out
- **UnexpectedError**: Unhandled exception occurred
- **ShutdownError**: Error during shutdown sequence
- **StatusRetrievalError**: Failed to get robot status

## Client Integration

### Subscribing to Robot Status SSE
```javascript
const eventSource = new EventSource('/api/robot/status');

eventSource.onmessage = (event) => {
  const status = JSON.parse(event.data);
  
  // Check for errors
  if (status.error_queue && status.error_queue.length > 0) {
    // Display recent errors
    status.error_queue.forEach(error => {
      console.error(`[${error.error_type}] ${error.message}`, error.context);
    });
  }
};
```

### Example Error Response
```json
{
  "robot_status": { ... },
  "running": true,
  "current_command": null,
  "error_queue": [
    {
      "timestamp": "2025-07-24T10:30:45.123456",
      "error_type": "TimeoutError",
      "message": "Operation timed out",
      "context": {
        "command": "move_to",
        "position": {"x": 100, "y": 200}
      },
      "stack_trace": null
    }
  ]
}
```

## Implementation Details

- Error queue is circular buffer limited to 10 errors
- Oldest errors are dropped when queue is full
- Errors persist until worker restart
- All SSE status updates include error queue
- Errors are captured at multiple points in command lifecycle