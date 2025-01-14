import z from 'zod';

export const stepperMotorSchema = z.object({
  microsteps: z.number(),
  steps_per_rev: z.number(),
  steps_per_mm: z.coerce.number(),
  work_current: z.number(),
  holding_current: z.number(),
  homing_current: z.number(),
  velocity: z.number(),
  acceleration: z.number(),
  encoder_ppr: z.coerce.number().nullable(),
  max_velocity: z.coerce.number().nullable(),
  max_acceleration: z.coerce.number().nullable(),
  min_travel: z.coerce.number().nullable(),
  max_travel: z.coerce.number().nullable(),
  invert_coordinates: z.boolean(),
  home_direction: z.enum(['CW', 'CCW']),
  homing_speed: z.number(),
  brake: z.boolean(),
  safety: z.boolean(),
  external_encoder: z.boolean(),
  max_encoder_deviation: z.number(),
  max_encoder_deviation_homing: z.number(),
});

export type StepperMotorSchema = z.infer<typeof stepperMotorSchema>;

export const axisSchema = z.object({
  can_id: z.number(),
  name: z.string(),
  axis_type: z.string(),
  configuration: stepperMotorSchema,
});

export type AxisSchema = z.infer<typeof axisSchema>;
