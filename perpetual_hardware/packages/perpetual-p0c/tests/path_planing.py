from pydantic import BaseModel
from perpetual_p0c.gantry import GantryCoordinate
from perpetual_p0c.configuration import p0_funnel_config, p0_gantry_config, p0_tool_config
from perpetual_p0c.gantry import GantryConfiguration, GantryCoordinate
from perpetual_p0c.funnel import FunnelConfiguration
from perpetual_p0c.tool import ToolConfiguration
from perpetual_p0c.configuration import p0_funnel_config, p0_gantry_config, p0_tool_config
from typing import List

class RobotConfiguration(BaseModel):
    funnel_configuration: FunnelConfiguration
    gantry_configuration: GantryConfiguration
    tool_configuration: ToolConfiguration
    gantry_safe_x: float
    gantry_safe_y: float
    gantry_safe_y_high_clearance: float
    place_offset: GantryCoordinate
    output_location: GantryCoordinate
    input_location: GantryCoordinate

def path_plan(coordinate: GantryCoordinate, current_coordinate: GantryCoordinate, config: RobotConfiguration) -> List[GantryCoordinate]:
    gantry_position = current_coordinate
    path: List[GantryCoordinate] = []
    # Target is beyod safe x
    if gantry_position.x > config.gantry_safe_x:
        print(gantry_position.y, config.gantry_safe_y_high_clearance)
        # Above High Clearance
        if gantry_position.y >= config.gantry_safe_y_high_clearance - 1:
            if coordinate.y >= gantry_position.y-1:
                path.append(GantryCoordinate(x=coordinate.x, y=coordinate.y))
                return path
            else:
                if coordinate.x < config.gantry_safe_x:
                    path.append(GantryCoordinate(x=config.gantry_safe_x, y=gantry_position.y))
                    path.append(GantryCoordinate(x=coordinate.x, y=coordinate.y))
                    return path
                else:
                    path.append(GantryCoordinate(x=config.gantry_safe_x, y=gantry_position.y))
                    path.append(GantryCoordinate(x=config.gantry_safe_x, y=config.gantry_safe_y))
                    path.append(GantryCoordinate(x=coordinate.x, y=config.gantry_safe_y))
                    path.append(GantryCoordinate(x=coordinate.x, y=coordinate.y))
                    return path
        # Below High Clearance
        else:
            if coordinate.y >= config.input_location.y+1:
                path.append(GantryCoordinate(x=gantry_position.x, y=config.gantry_safe_y))
                path.append(GantryCoordinate(x=config.gantry_safe_x, y=config.gantry_safe_y))
                path.append(GantryCoordinate(x=config.gantry_safe_x, y=coordinate.y))
                path.append(GantryCoordinate(x=coordinate.x, y=coordinate.y))
                return path
            else:
                path.append(GantryCoordinate(x=gantry_position.x, y=config.gantry_safe_y))
                path.append(GantryCoordinate(x=config.gantry_safe_x, y=config.gantry_safe_y))
                path.append(GantryCoordinate(x=coordinate.x, y=config.gantry_safe_y))
                path.append(GantryCoordinate(x=coordinate.x, y=coordinate.y))
                return path
    else:
        if coordinate.x >= config.gantry_safe_x:
            if coordinate.y >= config.gantry_safe_y_high_clearance-1:
                path.append(GantryCoordinate(x=gantry_position.x, y=coordinate.y))
                path.append(GantryCoordinate(x=coordinate.x, y=coordinate.y))
                return path
            else:
                path.append(GantryCoordinate(x=gantry_position.x, y=config.gantry_safe_y))
                path.append(GantryCoordinate(x=coordinate.x, y=config.gantry_safe_y))
                path.append(GantryCoordinate(x=coordinate.x, y=coordinate.y))
                return path
        else:
            path.append(coordinate)
            return path

if __name__ == '__main__':
    tray_1_coordinate = GantryCoordinate(x=175, y= 532)
    tray_2_coordinate = GantryCoordinate(x=245, y= 532)
    idle_coordinate = GantryCoordinate(x=100, y=100)
    robot_configuration = RobotConfiguration(
        funnel_configuration=p0_funnel_config,
        gantry_configuration=p0_gantry_config,
        tool_configuration=p0_tool_config,
        gantry_safe_x=320,
        gantry_safe_y=100,
        gantry_safe_y_high_clearance=1050,
        place_offset=GantryCoordinate(x=0, y=2),
        output_location=GantryCoordinate(x=676,y=1050),
        input_location=GantryCoordinate(x=676,y=288),
    )
    output_to_input = path_plan(
        robot_configuration.input_location,
        robot_configuration.output_location,
        robot_configuration
    )
    print("Output to Input")
    print(output_to_input)
    input_to_output = path_plan(
        robot_configuration.output_location,
        robot_configuration.input_location,
        robot_configuration
    )
    print("Input to Output")
    print(input_to_output)
    input_to_input = path_plan(
        robot_configuration.input_location,
        robot_configuration.input_location,
        robot_configuration
    )
    print("Input to Input")
    print(input_to_input)
    output_to_output = path_plan(
        robot_configuration.output_location,
        robot_configuration.output_location,
        robot_configuration
    )
    print("Output to Output")
    print(output_to_output)
    idle_to_high = path_plan(
        robot_configuration.output_location,
        idle_coordinate,
        robot_configuration
    )
    print("Idle to High")
    print(idle_to_high)

    idle_to_input = path_plan(
        robot_configuration.input_location,
        idle_coordinate,
        robot_configuration
    )
    print("Idle to Input")
    print(idle_to_input)

    tray_to_output = path_plan(
        robot_configuration.output_location,
        tray_1_coordinate,
        robot_configuration
    )
    print("Tray to High")
    print(idle_to_high)
    idle_to_input = path_plan(
        robot_configuration.input_location,
        tray_1_coordinate,
        robot_configuration
    )
    print("Tray to Input")
    print(idle_to_input)

    print("Output to Tray")
    output_to_tray = path_plan(
        robot_configuration.output_location,
        idle_coordinate,
        robot_configuration
    )
    print(output_to_tray)
