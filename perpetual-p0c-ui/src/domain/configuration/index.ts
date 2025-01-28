import z from 'zod';
import { axisSchema } from './axis';
import { gantryCoordinateSchema } from '../movement';

export const funnelConfigurationSchema = z.object({
  inlet_funnel: axisSchema,
  opposing_funnel: axisSchema,
  slot_1: axisSchema,
  slot_2: axisSchema,
  slot_3: axisSchema,
  belt: axisSchema,
  wall: axisSchema,
  output_transfer_conveyor: axisSchema,
  output_conveyor: axisSchema,
  diverter: axisSchema,
  funnel_min_width: z.number(),
  funnel_cell_1_offset_x: z.number(),
  catch_position: z.number(),
  diverter_50mm_pos: z.number(),
  diverter_100mm_pos: z.number(),
  diverter_150mm_pos: z.number(),
  belt_speed: z.number(),
  wall_speed: z.number(),
  output_transfer_conveyor_speed: z.number(),
  output_conveyor_speed: z.number(),
  slot_gap_size: z.number(),
  opposing_funnel_max_travel: z.number(),
  input_funnel_max_travel: z.number(),
  minimum_transfer_location: z.number(),
});

export const outputBoxConfigurationSchema = z.object({
  door: axisSchema,
  door_open_position: z.number(),
});

export const workstationConfigurationSchema = z.object({
  outputbox_1_configuration: outputBoxConfigurationSchema,
  outputbox_2_configuration: outputBoxConfigurationSchema,
  outputbox_3_configuration: outputBoxConfigurationSchema,
  input_belt_configuration: axisSchema,
  output_belt_configuration: axisSchema,
  input_belt_speed: z.number(),
  output_belt_speed: z.number(),
});

export const gantryConfigurationSchema = z.object({
  x_axis: axisSchema,
  y_axis: axisSchema,
});

export const toolConfigurationSchema = z.object({
  carriage: axisSchema,
  tool_retract_position: z.number(),
  tool_extend_position: z.number(),
  tool_safe_min: z.number(),
  tool_safe_max: z.number(),
  tray_pull_offset: z.number(),
  tray_pull_right: z.number(),
  tray_pull_left: z.number(),
});

export const robotConfigurationSchema = z.object({
  funnel_configuration: funnelConfigurationSchema,
  gantry_configuration: gantryConfigurationSchema,
  tool_configuration: toolConfigurationSchema,
  workstation: workstationConfigurationSchema,
  gantry_safe_x: z.number(),
  gantry_safe_y: z.number(),
  gantry_safe_y_high_clearance: z.number(),
  place_offset: gantryCoordinateSchema,
  service_coordinate_high: gantryCoordinateSchema,
  service_coordinate_low: gantryCoordinateSchema,
  idle_coordinate: gantryCoordinateSchema,
  output_location: gantryCoordinateSchema,
  input_location: gantryCoordinateSchema,
});
export type RobotConfiguration = z.infer<
  typeof robotConfigurationSchema
>;
