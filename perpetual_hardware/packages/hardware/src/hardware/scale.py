from hardware.ModbusController import SharedModbusController
from typing import Dict, Any

class Scale():
    def __init__(self, modbus_controller: SharedModbusController, scale_id: int):
        self.modbus_controller = modbus_controller
        self.scale_id = scale_id

    async def connect(self) -> bool:
        await self.modbus_controller.connect()
        return self.modbus_controller.client.connected

    async def close(self) -> None:
        await self.modbus_controller.close()

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

    async def read_weight_data(self) -> Dict[str, Any]:
        try:
            # TODO implement multi register read
            dec = await self.modbus_controller.read_holding_registers(0x1, 0x2)
            w1 = await self.modbus_controller.read_holding_registers(0x1, 0x0)
            w2 = await self.modbus_controller.read_holding_registers(0x1, 0x1)
            w = abs(w1-w2)/(10**dec)
            return w

            # decimal_pos = regs[2]
            # weight = abs(regs[0] - regs[1])
            # actual_weight = weight / (10 ** decimal_pos)
            # zero_point, stability, overload, bits_3_7, unit_name = await self.interpret_balance_status(regs[3])
            # return {
            #     'weight': actual_weight,
            #     'decimal_position': decimal_pos,
            #     'balance_status': {
            #         'zero_point': zero_point,
            #         'stability': stability,
            #         'overload': overload,
            #         'unit': unit_name
            #     },
            #     'raw_registers': regs
            # }
        except Exception as e:
            raise Exception(f"Error reading weight data: {e}")


    async def tare(self):
        await self.modbus_controller.write_single_register(self.scale_id, 0x4, 0x1)
        pass
