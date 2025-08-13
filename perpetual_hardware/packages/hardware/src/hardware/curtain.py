from hardware.ModbusController import SharedModbusController as Controller
from typing import Optional, List

class Curtain:
    def __init__(self, bus: str, address: int) -> None:
        self.controller: Controller = Controller(bus)
        self.address: int = address

    async def connect(self):
        if not await self.controller.connect():
            raise ConnectionError(f"Failed to connect to Intelligent Grating on {self.controller.port}")
        return True

    def close(self) -> None:
        self.controller.close()

    async def read_lowest_blocked(self) -> int:
        rr = await self.controller.read_holding_registers(self.address, 0x40)
        return rr.registers[0] 

    async def read_highest_blocked(self) -> int:
        rr = await self.controller.read_holding_registers(self.address, 0x41)
        return rr.registers[0] 

    async def read_blocked_quantity(self) -> int:
        rr = await self.controller.read_holding_registers(self.address, 0x42)
        return rr.registers[0] 

    async def read_all_light_status(self, count_light_points: int = 22) -> List[int]:
        reg_count: int = (count_light_points + 15) // 16
        rr = await self.controller.read_holding_registers(self.address, 0x0, reg_count)
        bits = []
        for ix, num in enumerate(rr.registers):
            swapped = ((num & 0xFF00) >> 8) | ((num & 0x00FF) << 8)
            for idx in range(0, 16): 
                if idx + ix * 16 >= count_light_points:
                    pass 
                else:
                    if swapped & (0x01 << idx):
                        bits.append(1)
                    else:
                        bits.append(0)
        return bits

