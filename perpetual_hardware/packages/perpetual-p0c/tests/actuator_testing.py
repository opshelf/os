from pydantic import BaseModel
from perpetual_p0c.gantry import GantryConfiguration, Gantry, GantryCoordinate
from perpetual_p0c.funnel import FunnelConfiguration, Funnel
from perpetual_p0c.tool import ToolConfiguration, Tool
from perpetual_p0c.storage import Tray, StorageModuleSide
from perpetual_p0c.configuration import p0_robot_config
from perpetual_p0c.utils import get_can_devices
from perpetual_p0c.robot import P0C
import time


tray_1_coordinate = GantryCoordinate(x=175, y= 532)
tray_2_coordinate = GantryCoordinate(x=245, y= 532)
idle_coordinate = GantryCoordinate(x=100, y=100)


if __name__ == '__main__':
    can_bus = get_can_devices()
    robot = P0C(p0_robot_config)
    robot.idle()
    robot.tool.actuators(True)
    robot.tool.actuators(False)
