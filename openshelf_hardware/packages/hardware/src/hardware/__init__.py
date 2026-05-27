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

iocam
    Time-of-Flight (TOF) camera interface for item sizing and detection

toolcam
    Tool-mounted camera interface for frame capture

motor_helpers
    Motor register definitions and constants
"""

from .controller import SharedModbusController
from .motor import Servo, MotorStatus
from .iocam import AFSCam
from .laser import LaserSensor
from .toolcam import ToolCamera

__all__ = [
    'SharedModbusController',
    'Servo',
    'MotorStatus',
    'AFSCam',
    'LaserSensor',
    'ToolCamera'
]
