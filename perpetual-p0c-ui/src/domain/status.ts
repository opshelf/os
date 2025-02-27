import { z } from 'zod';
import { robotCommandsSchema } from './commands/robot';

export const statusFlagsSchema = z.object({
  value: z.number(),
  stall_guard2_status: z.boolean().default(false),
  over_temperature: z.boolean().default(false),
  over_temperature_pre_warning: z.boolean().default(false),
  short_to_ground_a: z.boolean().default(false),
  short_to_ground_b: z.boolean().default(false),
  open_load_a: z.boolean().default(false),
  open_load_b: z.boolean().default(false),
  stand_still: z.boolean().default(false),
});
export type StatusFlags = z.infer<typeof statusFlagsSchema>;

export const trinamicErrorFlagsSchema = z.object({
  value: z.number(),
  stall_guard_error: z.boolean().default(false),
  deviation_error: z.boolean().default(false),
});
export type TrinamicErrorFlags = z.infer<
  typeof trinamicErrorFlagsSchema
>;

export const axisStatusSchema = z.object({
  status_flags: statusFlagsSchema,
  error_flags: trinamicErrorFlagsSchema,
  position: z.number(),
  homed: z.number(),
  hard_stopped: z.number(),
});
export type AxisStatus = z.infer<typeof axisStatusSchema>;

export const stepperStatusSchema = z.object({
  status_flags: statusFlagsSchema,
  error_flags: trinamicErrorFlagsSchema,
  position: z.number(),
  homed: z.number(),
  hard_stopped: z.number(),
});
export type StepperStatus = z.infer<typeof stepperStatusSchema>;

export const toolStatusSchema = z.object({
  puller_solenoids: z.number(),
  carriage_solenoids: z.number(),
  actuators: z.boolean(),
  carriage_status: axisStatusSchema,
});
export type ToolStatus = z.infer<typeof toolStatusSchema>;

export const outputBoxStatusSchema = z.object({
  door_status: axisStatusSchema,
});
export type OutputBoxStatus = z.infer<typeof outputBoxStatusSchema>;

export const gantryStatusSchema = z.object({
  x_axis: axisStatusSchema,
  y_axis: axisStatusSchema,
});
export type GantryStatus = z.infer<typeof gantryStatusSchema>;

export const funnelStatusSchema = z.object({
  inlet_funnel: axisStatusSchema,
  opposing_funnel: axisStatusSchema,
  slot_1: axisStatusSchema,
  slot_2: axisStatusSchema,
  slot_3: axisStatusSchema,
  belt: stepperStatusSchema,
  wall: stepperStatusSchema,
  diverter: stepperStatusSchema,
  output_transfer_conveyor: stepperStatusSchema,
  output_conveyor: stepperStatusSchema,
});
export type FunnelStatus = z.infer<typeof funnelStatusSchema>;

export const workstationStatusSchema = z.object({
  output_box_1: outputBoxStatusSchema,
  output_box_2: outputBoxStatusSchema,
  output_box_3: outputBoxStatusSchema,
  input_belt: axisStatusSchema,
  output_belt: axisStatusSchema,
});
export type WorkstationStatus = z.infer<
  typeof workstationStatusSchema
>;

export const sensorStatusSchema = z.object({
  output_conveyor: z.boolean(),
  input_conveyor: z.boolean(),
  output_transfer_conveyor: z.boolean(),
  funnel_input: z.boolean(),
  output_box_1: z.boolean(),
  output_box_2: z.boolean(),
  output_box_3: z.boolean(),
});

export type SensorStatus = z.infer<typeof sensorStatusSchema>;

export const robotStatusSchema = z.object({
  gantry_status: gantryStatusSchema,
  workstation_status: workstationStatusSchema,
  funnel_status: funnelStatusSchema,
  tool_status: toolStatusSchema,
  sensor_status: sensorStatusSchema.nullable(),
});

export type RobotStatus = z.infer<typeof robotStatusSchema>;

export const workerStatusSchema = z.object({
  robot_status: robotStatusSchema.nullable(),
  current_command: robotCommandsSchema.nullable(),
  running: z.boolean(),
});

export type WorkerStatus = z.infer<typeof workerStatusSchema>;
