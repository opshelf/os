#!/usr/bin/env python3
# /// script
# dependencies = ["pymodbus"]
# ///

import sys
import asyncio
from pymodbus.client import AsyncModbusSerialClient
import pymodbus

PORT = "/dev/ttyUSB0"
BAUDRATE = 115200
TRIGGER = 0x6002

MOTOR_ID = 5
MPR = 80
RPM = 500
ACCEL = 500

async def main():
	motor_id = int(sys.argv[1]) if len(sys.argv) > 1 else MOTOR_ID
	position_mm = float(sys.argv[2]) if len(sys.argv) > 2 else 100.0

	client = AsyncModbusSerialClient(
		port=PORT,
		baudrate=BAUDRATE,
		timeout=2.0,
		parity="N",
		bytesize=8,
		stopbits=1,
		framer=pymodbus.FramerType.RTU
	)

	connected = await client.connect()
	if connected is False:
		print(f"failed to connect to {PORT}")
		sys.exit(1)

	await client.write_register(0x1801, 0x1111, device_id=motor_id)
	await client.write_register(0x1801, 0x1122, device_id=motor_id)

	await client.write_register(0x6200, 0x1, device_id=motor_id)
	await client.write_register(0x6203, RPM, device_id=motor_id)
	await client.write_register(0x6204, ACCEL, device_id=motor_id)
	await client.write_register(0x6205, ACCEL, device_id=motor_id)

	ppr = 10000
	pulses = int((position_mm / MPR) * ppr)
	pulse_high = (pulses >> 16) & 0xFFFF
	pulse_low = pulses & 0xFFFF

	await client.write_register(0x6201, pulse_high, device_id=motor_id)
	await client.write_register(0x6202, pulse_low, device_id=motor_id)

	await client.write_register(TRIGGER, 0x10, device_id=motor_id)
	print(f"{motor_id} -> {position_mm}mm ({pulses} pulses)")

	client.close()

if __name__ == "__main__":
	asyncio.run(main())
