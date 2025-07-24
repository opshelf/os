# from perpetual_os1.storage import StorageGrid, Dimensions, CellStatus, Cell, CalibrationCoordinate, Shelf
from pydantic import BaseModel
from perpetual_os1.common import AxisCoordinate, ToolCoordinate

#5 steppers/1 servo
# all motors share a modbus object that maintains the connection

# todo list of dicts to be updatable on json
motor_stage     = {"motor_name":"stage", "motor_id":3, "Ip": 0xF, "rpm":0xA0, "nrev": 0x2600, "dir": 0, "mpr":50} # todo make the conversion to mm/rev
motor_gripper   = {"motor_name":"gripper", "motor_id":1, "Ip": 0xF, "rpm":0xF0, "nrev": 0xF000, "dir": 1, "mpr":19.992}
motor_rotate    = {"motor_name":"rotate", "motor_id":4, "Ip": 0x24, "rpm":0xF0, "nrev": 0x5800, "dir": 0, "mpr":36} # deg, not mm
motor_x         = {"motor_name":"x_axis", "motor_id":5, "Ip": 0x24, "rpm":0x80, "nrev": 0x8000, "dir": 0, "mpr":80}
# motor_y         = {"motor_id":0x05, "Ip": 0x0A, "rpm":0x40, "p_lim": 0x2600}
GRIPPER         = 0x09

class RobotConfiguration(BaseModel):
    # motor_list: list
    idle_position: AxisCoordinate
    place_offset: AxisCoordinate
    pickup_location: AxisCoordinate
    pickup_location_offset: AxisCoordinate
    # default_tool: ToolCoordinate
    # state_file: str = "state.json"

robot_configuration = RobotConfiguration(
    # motor_list
    idle_position=AxisCoordinate(x=20, y=20),
    place_offset=AxisCoordinate(x=0, y=3),
    pickup_location=AxisCoordinate(x=540,y=396),
    pickup_location_offset=AxisCoordinate(x=0, y=200),
)

# storage_grid = StorageGrid(
#     right_module=[
#         Shelf(
#             shelf_height=200,
#             shelf_width=1000,
#             cells=[
#                 Cell(
#                     status=CellStatus.EMPTY,
#                     item=None,
#                     calibration_point=CalibrationCoordinate(x=293, y=410, z=0),
#                     dimensions=Dimensions(length=145, height=200, width=145),
#                 ),
#                 Cell(
#                     status=CellStatus.EMPTY,
#                     item=None,
#                     calibration_point=None,
#                     dimensions=Dimensions(length=145, height=200, width=145),
#                 ),
#                 Cell(
#                     status=CellStatus.EMPTY,
#                     item=None,
#                     calibration_point=None,
#                     dimensions=Dimensions(length=145, height=200, width=145),
#                 ),
#                 Cell(
#                     status=CellStatus.EMPTY,
#                     item=None,
#                     calibration_point=None,
#                     dimensions=Dimensions(length=145, height=200, width=145),
#                 ),
#                 Cell(
#                     status=CellStatus.EMPTY,
#                     item=None,
#                     calibration_point=None,
#                     dimensions=Dimensions(length=145, height=200, width=145),
#                 ),
#             ],
#         ),
#         Shelf(
#             shelf_height=200,
#             shelf_width=1000,
#             cells=[
#                 Cell(
#                     status=CellStatus.EMPTY,
#                     item=None,
#                     calibration_point=CalibrationCoordinate(x=271.5, y=680, z=0),
#                     dimensions=Dimensions(length=100, height=200, width=100),
#                 ),
#                 Cell(
#                     status=CellStatus.EMPTY,
#                     item=None,
#                     calibration_point=None,
#                     dimensions=Dimensions(length=100, height=200, width=100),
#                 ),
#                 Cell(
#                     status=CellStatus.EMPTY,
#                     item=None,
#                     calibration_point=None,
#                     dimensions=Dimensions(length=100, height=200, width=100),
#                 ),
#                 Cell(
#                     status=CellStatus.EMPTY,
#                     item=None,
#                     calibration_point=None,
#                     dimensions=Dimensions(length=100, height=200, width=100),
#                 ),
#             ],
#         ),
#         Shelf(
#             shelf_height=200,
#             shelf_width=1000,
#             cells=[
#                 Cell(
#                     status=CellStatus.EMPTY,
#                     item=None,
#                     calibration_point=CalibrationCoordinate(x=246.5, y=885, z=0),
#                     dimensions=Dimensions(length=50, height=100, width=50),
#                 ),
#                 Cell(
#                     status=CellStatus.EMPTY,
#                     item=None,
#                     calibration_point=None,
#                     dimensions=Dimensions(length=50, height=100, width=50),
#                 ),
#                 Cell(
#                     status=CellStatus.EMPTY,
#                     item=None,
#                     calibration_point=None,
#                     dimensions=Dimensions(length=50, height=100, width=50),
#                 ),
#                 Cell(
#                     status=CellStatus.EMPTY,
#                     item=None,
#                     calibration_point=None,
#                     dimensions=Dimensions(length=50, height=100, width=50),
#                 ),
#                 Cell(
#                     status=CellStatus.EMPTY,
#                     item=None,
#                     calibration_point=None,
#                     dimensions=Dimensions(length=50, height=100, width=50),
#                 ),
#             ],
#         ),
#     ],
#     left_module=[],
# )