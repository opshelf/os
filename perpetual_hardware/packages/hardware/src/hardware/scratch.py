from pymodbus.client import ModbusSerialClient as ModbusClient
import time
slave_id = 5

client = ModbusClient(
    port="/dev/ttyACM0",
    baudrate=115200,
    timeout=1,
    stopbits=1,
    bytesize=8,
    parity='N'  # Use 'E' or 'O' if your device uses even/odd parity
)

if client.connect():
    print("connected")
    client.write_register(0x3, 0x2, slave=slave_id)
    client.write_register(0x1801, 0x1111, slave=slave_id) # clear errors
    client.write_register(0x1801, 0x1122, slave=slave_id) # clear errors
    client.write_register(0x00B, 4000, slave=slave_id)
    client.write_register(0x0159, 0xA1, slave=slave_id)
    client.write_register(0x0167, 250, slave=slave_id)
    client.write_register(0x0169, 250, slave=slave_id)
    client.write_register(0x016B, 10, slave=slave_id)
    client.write_register(0x0007, 0, slave=slave_id) 
    client.write_register(0x6200, 0x41, slave=slave_id)
    client.write_register(0x0191, 0x46, slave=slave_id)
    client.write_register(0x6203, 0xFF, slave=slave_id)
    client.write_register(0x6204, 0x32, slave=slave_id)
    client.write_register(0x6205, 0x32, slave=slave_id)
    client.write_register(0x6202, 10000, slave=slave_id)
    client.write_register(0x6002, 0x10, slave=slave_id)
    # time.sleep(2)
    # client.write_register(0x6002, 0x40, slave=slave_id)






