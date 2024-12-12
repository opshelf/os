from perpetual_p0c.common import StorageModuleSide
from perpetual_p0c.configuration import p0_tool_config
from perpetual_p0c.utils import get_can_devices
from perpetual_p0c.tool_board import ToolBoard, IoBoard
from perpetual_p0c.tool import Tool
from can import Bus
import time

def home_tool(tool: Tool) -> None:
    tool.home()

def test_puller(tool: Tool) -> None:
    for i in range(0,6):
        print(f"{bin(1 << i )}")
        tool.set_slot_bits(1 << i)
        input()
        tool.actuators(True)
        time.sleep(2)
        print(tool.tool_board.get_state().slots)
        input()
        tool.actuators(False)
        input()
    for i in range(0,5):
        print(f"{bin((1 << i) | (1 << i+1) )}")
        tool.set_slot_bits((1 << i) | (1 << i+1))
        input()
        tool.actuators(True)
        time.sleep(2)
        print(tool.tool_board.get_state().slots)
        input()
        tool.actuators(False)
        input()
    for i in range(0,4):
        print(f"{bin(1 << i | 1 << i+1 | 1 << i+2 )}")
        tool.set_slot_bits((1 << i) | (1 << i+1) | (1 << i+2) )
        input()
        tool.actuators(True)
        time.sleep(2)
        print(tool.tool_board.get_state().slots)
        input()
        tool.actuators(False)
        input()

def test_carriage(tool: Tool) -> None:
    for i in range(0,3):
        print(f"Left Side: {i}")
        tool.set_carriage_solenoids(i, StorageModuleSide.LEFT)
        time.sleep(1)
        #input()
    for i in range(0,3):
        print(f"Right Side: {i}")
        tool.set_carriage_solenoids(i, StorageModuleSide.RIGHT)
        time.sleep(1)
        #input()

def test_tool_inputs() -> None:
    can_bus = get_can_devices()
    with Bus(
        interface=can_bus.CAN_INTERFACE,
        channel=can_bus.CAN_CHANNEL,
        bitrate=can_bus.CAN_BITRATE,
        fd=False,
    ) as bus:
        tool = ToolBoard(bus)
        while True:
            state = tool.get_state()
            print(state.inputs)
            time.sleep(1)


if __name__ == "__main__":
    #test_tool_inputs()
    can_bus = get_can_devices()
    with Bus(
        interface=can_bus.CAN_INTERFACE,
        channel=can_bus.CAN_CHANNEL,
        bitrate=can_bus.CAN_BITRATE,
        fd=False,
    ) as bus:
        #tool = Tool(bus, p0_tool_config)
        #tool.actuators(False)
        #tool.home()
        #tool.puller.move_to_position(480)
        #tool.set_carriage_solenoids(1, StorageModuleSide.RIGHT)
        tool_board = ToolBoard(bus)
        print(tool_board.set_carriage(IoBoard(r1=True)))
        print(input())
        print(tool_board.set_carriage(IoBoard(r2=True)))
        print(input())
        print(tool_board.set_carriage(IoBoard(r3=True)))
        print(input())
        print(tool_board.set_carriage(IoBoard(r4=True)))
        print(input())
        print(tool_board.set_carriage(IoBoard(r5=True)))
        print(input())
        print(tool_board.set_carriage(IoBoard(r6=True)))
        print(input())
        #home_tool(tool) 
        #while True:
        #    test_carriage(tool)
        #test_puller(tool)


        
