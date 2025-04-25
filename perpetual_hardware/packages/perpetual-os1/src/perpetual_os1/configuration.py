from perpetual_os1.common import CanMessageIds, CanAxisConfiguration
from hardware_common.stepper_motor import StepperMotorConfig, Rotation
from perpetual_os1.gantry import GantryConfiguration, GantryCoordinate, Gantry
from perpetual_os1.tool import ToolConfiguration, GripperConfiguration
from perpetual_os1.robot import RobotConfiguration


x_axis = CanAxisConfiguration(
    can_id=CanMessageIds.X_AXIS.value,
    configuration=StepperMotorConfig(
        microsteps=16,
        steps_per_rev=200,
        encoder_ppr=4000,
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
        invert_coordinates=False,
        home_direction=Rotation.CCW,
        homing_speed=50,
        external_encoder=True,
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
        velocity=400,
        acceleration=100,
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

gripper_axis_configuration = CanAxisConfiguration(
    can_id=CanMessageIds.GRIPPER.value,
    configuration=StepperMotorConfig(
        microsteps=16,
        steps_per_rev=200,
        encoder_ppr=4000,
        steps_per_mm=63.66,
        work_current=2000,
        holding_current=0,
        homing_current=3000,
        velocity=200,
        acceleration=100,
        max_velocity=200,
        max_acceleration=50,
        min_travel=None,
        max_travel=150,
        invert_coordinates=False,
        home_direction=Rotation.CCW,
        homing_speed=50,
        brake=True,
        external_encoder=True,
    ),
    name="gripper",
    axis_type="leadscrew",
)

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


gripper_configuration = GripperConfiguration()

tool_configuration = ToolConfiguration(
    gripper=gripper_configuration,
    z_axis=z_axis_configuration,
    tool_shelf_offset=7.0,
    tool_retracted_offset=1.0,
    tool_extension_offset=158,
    tool_pickup_extension=152,
)

robot_configuration = RobotConfiguration(
    tool=tool_configuration,
    gantry=gantry_configuration,
    y_safe=10.0,
    idle_position=GantryCoordinate(x=20, y=20),
    place_offset=GantryCoordinate(x=0, y=2),
    # pickup_location=GantryCoordinate(x=540,y=396),
    pickup_location=GantryCoordinate(x=733.25, y=475),
    pickup_location_offset=GantryCoordinate(x=0, y=250),
)
