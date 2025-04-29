import z from "zod";
import { axisSchema } from "./axis";
import { gantryCoordinateSchema } from "../movement";

export const gantryConfigurationSchema = z.object({
  x_axis: axisSchema,
  y_axis: axisSchema,
});

export const gripperConfigurationSchema = z.object({
  min_width: z.number(),
  max_width: z.number(),
  gripper_pickup_width: z.number(),
});

export const toolConfigurationSchema = z.object({
  gripper: gripperConfigurationSchema,
  z_axis: axisSchema,
  tool_shelf_offset: z.number(),
  tool_retracted_offset: z.number(),
  tool_extension_offset: z.number(),
  tool_pickup_extension: z.number(),
});

// RobotConfiguration full schema
export const robotConfigurationSchema = z.object({
  y_safe: z.number(),
  idle_position: gantryCoordinateSchema,
  place_offset: gantryCoordinateSchema,
  pickup_location: gantryCoordinateSchema,
  pickup_location_offset: gantryCoordinateSchema,
  state_file: z.string(),
  tool: toolConfigurationSchema,
  gantry: gantryConfigurationSchema,
});

export type RobotConfiguration = z.infer<typeof robotConfigurationSchema>;
