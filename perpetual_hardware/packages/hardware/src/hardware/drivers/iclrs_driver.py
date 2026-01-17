from hardware.controller import SharedModbusController
from hardware.device_registers import DeviceRegisters

class Motor():
  _motor_id: int
  _config: dict

  def __init__(self,  config):
    self._motor_id = config['motor_id']
    self._device_registers = DeviceRegisters()

  async def configure(self):
    configuration_registers = self._config['initial_registers']
    for reg in configuration_registers:

