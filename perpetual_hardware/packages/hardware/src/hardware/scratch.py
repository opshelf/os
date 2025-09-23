from pymodbus.client import ModbusSerialClient as ModbusClient
import time
from typing import Optional, Dict, Any

# Import or define the Scale class (copied from curtain_scale_monitor.py)
class Scale:
    """Scale device class"""
    def __init__(self, client: ModbusClient, address: int = 1) -> None:
        self.client: ModbusClient = client
        self.address: int = address

    @classmethod
    def build(cls, port: str, address: int = 1, baudrate: int = 9600, 
              parity: str = 'N', timeout: int = 2, stopbits: int = 1) -> 'Scale':
        client: ModbusClient = ModbusClient(
            port=port,
            baudrate=baudrate,
            parity=parity,
            stopbits=stopbits,
            bytesize=8,
            timeout=timeout
        )
        instance: Scale = cls(client=client, address=address)
        return instance

    def connect(self) -> bool:
        return self.client.connect()

    def close(self) -> None:
        self.client.close()

    def read_registers(self, address: int, count: int = 1) -> list:
        response = self.client.read_holding_registers(address=address, count=count, slave=self.address)
        if response.isError():
            raise Exception(f"Error reading registers at {address}: {response}")
        return response.registers

    def interpret_balance_status(self, value: int):
        zero_point = '0' if (value & 0x01) else '1'
        stability = 'stability' if (value & 0x02) else 'instability'
        overload = 'overload' if (value & 0x04) else 'normal'
        bits_3_7 = (value >> 3) & 0x1F
        unit_map = {
            0: "ct", 1: "lb", 2: "oz", 3: "ozt", 4: "dr", 5: "lbt", 6: "gr", 7: "dwt",
            8: "K.tl", 9: "T.tl", 10: "H.tl", 11: "S.tl", 12: "T", 13: "M", 14: "Cl", 15: "Mom",
            16: "BG", 17: "kg", 18: "mg", 19: "g"
        }
        unit_name = unit_map.get(bits_3_7, f"Unknown ({bits_3_7})")
        return zero_point, stability, overload, bits_3_7, unit_name

    def read_weight_data(self) -> Dict[str, Any]:
        try:
            regs = self.read_registers(0, 10)
            decimal_pos = regs[2]
            weight = abs(regs[0] - regs[1])
            actual_weight = weight / (10 ** decimal_pos)
            zero_point, stability, overload, bits_3_7, unit_name = self.interpret_balance_status(regs[3])
            return {
                'weight': actual_weight,
                'decimal_position': decimal_pos,
                'balance_status': {
                    'zero_point': zero_point,
                    'stability': stability,
                    'overload': overload,
                    'unit': unit_name
                },
                'raw_registers': regs
            }
        except Exception as e:
            raise Exception(f"Error reading weight data: {e}")

# MotorController (adapted from curtain_motor.py)
class MotorController:
    def __init__(self, client: ModbusClient, slave_id: int = 2):
        self.client = client
        self.slave_id = slave_id
    
    def run_motor(self, duration: int = 20):
        if not self.client:
            print("❌ Motor controller not connected")
            return False
        try:
            print("🔧 Configuring motor parameters...")
            self.client.write_register(0x1801, 0x1111, slave=self.slave_id)
            self.client.write_register(0x1801, 0x1122, slave=self.slave_id)
            self.client.write_register(0x6200, 0x02, slave=self.slave_id)
            self.client.write_register(0x0007, 0x1, slave=self.slave_id)
            self.client.write_register(0x6203, 0x100, slave=self.slave_id)
            self.client.write_register(0x6202, 0xFFFF, slave=self.slave_id)
            print("🚀 Starting motor...")
            self.client.write_register(0x6002, 0x10, slave=self.slave_id)
            print(f"⏱️  Running motor for {duration} seconds...")
            time.sleep(duration)
            print("🛑 Stopping motor...")
            self.client.write_register(0x6002, 0x40, slave=self.slave_id)
            print("✅ Motor operation completed")
            return True
        except Exception as e:
            print(f"❌ Motor operation failed: {e}")
            return False

def print_scale_status(status: dict):
    print("=" * 60)
    print("⚖️ SCALE STATUS")
    print("=" * 60)
    if status is None:
        print("❌ Failed to read scale status")
        return
    print(f"Weight: {status['weight']} {status['balance_status']['unit']}")
    print(f"Decimal Position: {status['decimal_position']}")
    print(f"Zero Point: {status['balance_status']['zero_point']}")
    print(f"Stability: {status['balance_status']['stability']}")
    print(f"Overload: {status['balance_status']['overload']}")
    print(f"Raw Registers: {status['raw_registers']}")
    print(f"⏰ Timestamp: {time.strftime('%H:%M:%S')}")
    print("=" * 60)

def main():
    print("🚀 Starting scale motor script...")
    # Use a single client for both scale and motor
    client = ModbusClient(
        port='/dev/ttyUSB0',
        baudrate=9600,
        timeout=1,
        stopbits=1,
        bytesize=8,
        parity='N'
    )
    if not client.connect():
        print("❌ Failed to connect to Modbus devices")
        return
    print("✅ Connected to Modbus devices")
    try:
        # Read and print scale status
        print("\n⚖️ Reading scale status...")
        scale = Scale(client=client, address=1)
        scale_status = scale.read_weight_data()
        print_scale_status(scale_status)
        # Run motor using same client
        # print("\n🔧 Initializing motor controller...")
        # time.sleep(1)
        # motor = MotorController(client=client, slave_id=2)
        # print("✅ Motor controller ready")
        # motor.run_motor(duration=20)
    except Exception as e:
        print(f"❌ Error during operation: {e}")
    finally:
        client.close()
        print("🔌 Modbus client disconnected")
    print("\n✅ Script completed!")

if __name__ == "__main__":
    main()