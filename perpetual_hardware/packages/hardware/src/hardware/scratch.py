from pymodbus.client import ModbusSerialClient as ModbusClient
slave_id = 5

client = ModbusClient(
    port="/dev/ttyUSB0",
    baudrate=115200,
    timeout=1,
    stopbits=1,
    bytesize=8,
    parity='N'  # Use 'E' or 'O' if your device uses even/odd parity
)

if client.connect():
    print("connected")
    # client.close()
    client.write_register(0x0147, 0x25,slave=slave_id) # pos limit
    client.write_register(0x6200, 0x41, slave=slave_id)
    client.write_register(0x0191, 0x32, slave=slave_id)
    client.write_register(0x6203, 0x1F0, slave=slave_id)
    client.write_register(0x6202, 0xFF, slave=slave_id)
    client.write_register(0x6002, 0x10, slave=slave_id)
