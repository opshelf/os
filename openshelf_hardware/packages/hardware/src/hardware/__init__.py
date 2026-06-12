"""
Hardware Control Package
======================

This package provides hardware control interfaces for the OpenShelf robot system.

Modules:
--------
controller
    Shared Modbus controller for serial communication with hardware devices

motor
    Motor control interface for servo motors using Modbus protocol

toolcam
    Tool-mounted camera interface for frame capture

motor_helpers
    Motor register definitions and constants
"""

from .controller import SharedModbusController
from .motor import MotorStatus
from .servo import Servo
from .laser import LaserSensor
from .toolcam import ToolCamera
from .uvc_camera import UvcCamera
from .tof import ToFReading, ToFSensorBus

__all__ = [
    'SharedModbusController',
    'Servo',
    'MotorStatus',
    'LaserSensor',
    'ToolCamera',
    'UvcCamera',
    'ToFReading',
    'ToFSensorBus',
]
