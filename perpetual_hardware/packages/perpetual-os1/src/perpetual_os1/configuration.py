from perpetual_os1.common import CanMessageIds, CanAxisConfiguration
from hardware_common.stepper_motor import StepperMotorConfig, Rotation
from perpetual_os1.door import DoorConfiguration
from perpetual_os1.gantry import GantryConfiguration, GantryCoordinate
from perpetual_os1.storage import StorageGrid, Dimensions, CellStatus, Cell, CalibrationCoordinate, Shelf
from perpetual_os1.tool import ToolConfiguration, GripperConfiguration
from perpetual_os1.robot import RobotConfiguration


x_axis = CanAxisConfiguration(
    can_id=CanMessageIds.X_AXIS.value,
    configuration=StepperMotorConfig(
        microsteps=16,
        steps_per_rev=200,
        encoder_ppr=3200,
        steps_per_mm=40,
        work_current=5000,
        holding_current=0,
        homing_current=4000,
        velocity=1000,
        acceleration=500,
        max_velocity=200,
        max_acceleration=200,
        min_travel=None,
        max_travel=None,
        invert_coordinates=True,
        home_direction=Rotation.CW,
        homing_speed=150,
    ),
    name="x_axis",
    axis_type="linear_axis",
)

y_axis = CanAxisConfiguration(
    can_id=CanMessageIds.Y_AXIS.value,
    configuration=StepperMotorConfig(
        microsteps=16,
        steps_per_rev=200,
        encoder_ppr=4000,
        steps_per_mm=40,
        work_current=5000,
        holding_current=5000,
        homing_current=2000,
        velocity=200,
        acceleration=50,
        max_velocity=200,
        max_acceleration=200,
        min_travel=None,
        max_travel=None,
        invert_coordinates=False,
        home_direction=Rotation.CCW,
        homing_speed=50,
        brake=True,
        external_encoder=True,
    ),
    name="y_axis",
    axis_type="linear_axis",
)

gantry_configuration = GantryConfiguration(x_axis=x_axis, y_axis=y_axis)

rotation_configuration = CanAxisConfiguration(
    can_id=CanMessageIds.ROTATION.value,
    configuration=StepperMotorConfig(
        microsteps=16,
        steps_per_rev=200,
        encoder_ppr=4000,
        steps_per_mm=266.667,
        work_current=2000,
        holding_current=0,
        homing_current=500,
        velocity=50,
        acceleration=50,
        max_velocity=200,
        max_acceleration=50,
        min_travel=None,
        max_travel=None,
        invert_coordinates=True,
        home_direction=Rotation.CW,
        homing_speed=50,
        external_encoder=True,
        brake=True
    ),
    name="stage",
    axis_type="leadscrew",
)

"""
z_axis_configuration = CanAxisConfiguration(
    can_id=CanMessageIds.Z_AXIS.value,
    configuration=StepperMotorConfig(
        microsteps=16,
        steps_per_rev=200,
        encoder_ppr=3200,
        steps_per_mm=266.667,
        work_current=2000,
        holding_current=0,
        homing_current=3000,
        velocity=1000,
        acceleration=1000,
        max_velocity=200,
        max_acceleration=50,
        min_travel=None,
        max_travel=None,
        invert_coordinates=False,
        home_direction=Rotation.CCW,
        homing_speed=50,
    ),
    name="z_axis",
    axis_type="leadscrew",
)
"""
z_axis_configuration = CanAxisConfiguration(
    can_id=CanMessageIds.Z_AXIS.value,
    configuration=StepperMotorConfig(
        microsteps=16,
        steps_per_rev=200,
        encoder_ppr=4000,
        steps_per_mm=160,
        work_current=2000,
        holding_current=0,
        homing_current=2000,
        velocity=100,
        acceleration=100,
        max_velocity=200,
        max_acceleration=50,
        min_travel=None,
        max_travel=None,
        invert_coordinates=False,
        home_direction=Rotation.CCW,
        homing_speed=50,
        external_encoder=True
    ),
    name="z_axis",
    axis_type="leadscrew",
)

door_axis_configuration = CanAxisConfiguration(
    can_id=CanMessageIds.DOOR.value,
    configuration=StepperMotorConfig(
        #microsteps=16,
        #steps_per_rev=200,
        #encoder_ppr=3200,
        #steps_per_mm=266.667,
        #work_current=2000,
        #holding_current=0,
        #homing_current=3000,
        #velocity=1000,
        #acceleration=1000,
        #max_velocity=200,
        #max_acceleration=50,
        #min_travel=None,
        #max_travel=None,
        #invert_coordinates=False,
        #home_direction=Rotation.CCW,
        #homing_speed=50,
        #brake=True
        microsteps=16,
        steps_per_rev=200,
        encoder_ppr=4000,
        steps_per_mm=400.0,
        work_current=2000,
        holding_current=0,
        homing_current=1000,
        velocity=100,
        acceleration=100,
        max_velocity=200,
        max_acceleration=50,
        min_travel=None,
        max_travel=337.52,
        invert_coordinates=False,
        home_direction=Rotation.CCW,
        homing_speed=50,
        external_encoder=True,
        brake=True
    ),


    name="door",
    axis_type="leadscrew",
)

door_configuration = DoorConfiguration(
    axis=door_axis_configuration, open_position=337.0, close_position=1.0
)


gripper_configuration = GripperConfiguration()

tool_configuration = ToolConfiguration(
    gripper=gripper_configuration,
    z_axis=z_axis_configuration,
    rotation=rotation_configuration,
    tool_shelf_offset=7.0,
    tool_retracted_offset=1.0,
    tool_extension_offset=165,
    tool_pickup_extension=159,
)

robot_configuration = RobotConfiguration(
    tool=tool_configuration,
    gantry=gantry_configuration,
    door=door_configuration,
    y_safe=10.0,
    idle_position=GantryCoordinate(x=20, y=20),
    place_offset=GantryCoordinate(x=0, y=3),
    # pickup_location=GantryCoordinate(x=540,y=396),
    pickup_location=GantryCoordinate(x=0.5, y=489),
    pickup_location_offset=GantryCoordinate(x=0, y=200),
)




storage_grid = StorageGrid(
    right_module=[
        Shelf(
            shelf_height=200,
            shelf_width=1000,
            cells=[
                Cell(
                    status=CellStatus.EMPTY,
                    item=None,
                    calibration_point=CalibrationCoordinate(x=293, y=410, z=0),
                    dimensions=Dimensions(length=145, height=200, width=145),
                ),
                Cell(
                    status=CellStatus.EMPTY,
                    item=None,
                    calibration_point=None,
                    dimensions=Dimensions(length=145, height=200, width=145),
                ),
                Cell(
                    status=CellStatus.EMPTY,
                    item=None,
                    calibration_point=None,
                    dimensions=Dimensions(length=145, height=200, width=145),
                ),
                Cell(
                    status=CellStatus.EMPTY,
                    item=None,
                    calibration_point=None,
                    dimensions=Dimensions(length=145, height=200, width=145),
                ),
                Cell(
                    status=CellStatus.EMPTY,
                    item=None,
                    calibration_point=None,
                    dimensions=Dimensions(length=145, height=200, width=145),
                ),
            ],
        ),
        Shelf(
            shelf_height=200,
            shelf_width=1000,
            cells=[
                Cell(
                    status=CellStatus.EMPTY,
                    item=None,
                    calibration_point=CalibrationCoordinate(x=271.5, y=680, z=0),
                    dimensions=Dimensions(length=100, height=200, width=100),
                ),
                Cell(
                    status=CellStatus.EMPTY,
                    item=None,
                    calibration_point=None,
                    dimensions=Dimensions(length=100, height=200, width=100),
                ),
                Cell(
                    status=CellStatus.EMPTY,
                    item=None,
                    calibration_point=None,
                    dimensions=Dimensions(length=100, height=200, width=100),
                ),
                Cell(
                    status=CellStatus.EMPTY,
                    item=None,
                    calibration_point=None,
                    dimensions=Dimensions(length=100, height=200, width=100),
                ),
            ],
        ),
        Shelf(
            shelf_height=200,
            shelf_width=1000,
            cells=[
                Cell(
                    status=CellStatus.EMPTY,
                    item=None,
                    calibration_point=CalibrationCoordinate(x=246.5, y=885, z=0),
                    dimensions=Dimensions(length=50, height=100, width=50),
                ),
                Cell(
                    status=CellStatus.EMPTY,
                    item=None,
                    calibration_point=None,
                    dimensions=Dimensions(length=50, height=100, width=50),
                ),
                Cell(
                    status=CellStatus.EMPTY,
                    item=None,
                    calibration_point=None,
                    dimensions=Dimensions(length=50, height=100, width=50),
                ),
                Cell(
                    status=CellStatus.EMPTY,
                    item=None,
                    calibration_point=None,
                    dimensions=Dimensions(length=50, height=100, width=50),
                ),
                Cell(
                    status=CellStatus.EMPTY,
                    item=None,
                    calibration_point=None,
                    dimensions=Dimensions(length=50, height=100, width=50),
                ),
            ],
        ),
    ],
    left_module=[],
)
