import { z } from 'zod';
import { robotCommandsSchema } from './commands/robot';

export const statusFlagsSchema = z.object({
  value: z.number(),
  stallGuard2Status: z.boolean().default(false),
  overTemperature: z.boolean().default(false),
  overTemperaturePreWarning: z.boolean().default(false),
  shortToGroundA: z.boolean().default(false),
  shortToGroundB: z.boolean().default(false),
  openLoadA: z.boolean().default(false),
  openLoadB: z.boolean().default(false),
  standStill: z.boolean().default(false),
});
export type StatusFlags = z.infer<typeof statusFlagsSchema>;

export const trinamicErrorFlagsSchema = z.object({
  value: z.number(),
  stallGuardError: z.boolean().default(false),
  deviationError: z.boolean().default(false),
});
export type TrinamicErrorFlags = z.infer<
  typeof trinamicErrorFlagsSchema
>;

export const axisStatusSchema = z.object({
  statusFlags: statusFlagsSchema,
  errorFlags: trinamicErrorFlagsSchema,
  position: z.number(),
  homed: z.number(),
  hardStopped: z.number(),
});
export type AxisStatus = z.infer<typeof axisStatusSchema>;

export const stepperStatusSchema = z.object({
  statusFlags: statusFlagsSchema,
  errorFlags: trinamicErrorFlagsSchema,
  position: z.number(),
  homed: z.number(),
  hardStopped: z.number(),
});
export type StepperStatus = z.infer<typeof stepperStatusSchema>;

export const toolStatusSchema = z.object({
  pullerSolenoids: z.number(),
  carriageSolenoids: z.number(),
  actuators: z.boolean(),
  carriageStatus: axisStatusSchema,
});
export type ToolStatus = z.infer<typeof toolStatusSchema>;

export const outputBoxStatusSchema = z.object({
  doorStatus: axisStatusSchema,
});
export type OutputBoxStatus = z.infer<typeof outputBoxStatusSchema>;

export const gantryStatusSchema = z.object({
  xAxis: axisStatusSchema,
  yAxis: axisStatusSchema,
});
export type GantryStatus = z.infer<typeof gantryStatusSchema>;

export const funnelStatusSchema = z.object({
  inletFunnel: axisStatusSchema,
  opposingFunnel: axisStatusSchema,
  slot1: axisStatusSchema,
  slot2: axisStatusSchema,
  slot3: axisStatusSchema,
  belt: stepperStatusSchema,
  wall: stepperStatusSchema,
  diverter: stepperStatusSchema,
  outputTransferConveyor: stepperStatusSchema,
  outputConveyor: stepperStatusSchema,
});
export type FunnelStatus = z.infer<typeof funnelStatusSchema>;

export const workstationStatusSchema = z.object({
  outputBox1: outputBoxStatusSchema,
  outputBox2: outputBoxStatusSchema,
  outputBox3: outputBoxStatusSchema,
  inputBelt: axisStatusSchema,
  outputBelt: axisStatusSchema,
});
export type WorkstationStatus = z.infer<
  typeof workstationStatusSchema
>;

export const robotStatusSchema = z.object({
  gantryStatus: gantryStatusSchema,
  workstationStatus: workstationStatusSchema,
  funnelStatus: funnelStatusSchema,
  toolStatus: toolStatusSchema,
});

export type RobotStatus = z.infer<typeof robotStatusSchema>;

export const workerStatusSchema = z.object({
  robot_status: robotStatusSchema.nullable(),
  current_command: robotCommandsSchema.nullable(),
  running: z.boolean(),
});

export type WorkerStatus = z.infer<typeof workerStatusSchema>;
