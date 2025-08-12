from pymodbus.client import ModbusSerialClient as ModbusClient
import time
slave_id = 2

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
    for i in range(10):
        client.write_register(0x1801, 0x1111, slave=slave_id) # clear errors
        client.write_register(0x1801, 0x1122, slave=slave_id) # clear errors
        client.write_register(0x6200, 0x41, slave=slave_id)
        client.write_register(0x0191, 0x46, slave=slave_id)
        client.write_register(0x6203, 0xF0, slave=slave_id)
        client.write_register(0x0007, 0, slave=slave_id) 
        client.write_register(0x6202, 0xFFFF, slave=slave_id)
        client.write_register(0x6002, 0x10, slave=slave_id)

        time.sleep(5)

        client.write_register(0x1801, 0x1111, slave=slave_id) # clear errors
        client.write_register(0x1801, 0x1122, slave=slave_id) # clear errors
        client.write_register(0x6200, 0x41, slave=slave_id)
        client.write_register(0x0191, 0x46, slave=slave_id)
        client.write_register(0x6203, 0xF0, slave=slave_id)
        client.write_register(0x0007, 1, slave=slave_id) 
        client.write_register(0x6202, 0xFFFF, slave=slave_id)
        client.write_register(0x6002, 0x10, slave=slave_id)

        time.sleep(5)

