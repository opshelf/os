import z from 'zod';
import { baseMessageSchema } from './base';

export const workstationShutdownCommandSchema =
  baseMessageSchema.extend({
    command: z.literal('shutdown'),
  });

export const workstationStopCommandSchema = baseMessageSchema.extend({
  command: z.literal('stop'),
});

export const workstationInitializeCommandSchema =
  baseMessageSchema.extend({
    command: z.literal('initialize'),
  });

export const workstationStartInputCommandSchema =
  baseMessageSchema.extend({
    command: z.literal('start_input_belt'),
  });

export const workstationStopInputCommandSchema =
  baseMessageSchema.extend({
    command: z.literal('stop_input_belt'),
  });

export const workstationStartOutputCommandSchema =
  baseMessageSchema.extend({
    command: z.literal('start_output_belt'),
  });

export const workstationStopOutputCommandSchema =
  baseMessageSchema.extend({
    command: z.literal('stop_output_belt'),
  });

export const workstationOpenOutputCommandSchema =
  baseMessageSchema.extend({
    command: z.literal('open_output'),
  });

export const workstationCloseOutputCommandSchema =
  baseMessageSchema.extend({
    command: z.literal('close_output'),
  });

export const workstationHomeOutputCommandSchema =
  baseMessageSchema.extend({
    command: z.literal('home_output'),
  });

// WorkstationCommands schema
export const workstationCommandsSchema = z.discriminatedUnion(
  'command',
  [
    workstationShutdownCommandSchema,
    workstationStopCommandSchema,
    workstationInitializeCommandSchema,
    workstationStartInputCommandSchema,
    workstationStopInputCommandSchema,
    workstationStartOutputCommandSchema,
    workstationStopOutputCommandSchema,
    workstationOpenOutputCommandSchema,
    workstationCloseOutputCommandSchema,
    workstationHomeOutputCommandSchema,
  ]
);

// Exported types
export type WorkstationShutdownCommand = z.infer<
  typeof workstationShutdownCommandSchema
>;
export type WorkstationStopCommand = z.infer<
  typeof workstationStopCommandSchema
>;
export type WorkstationInitializeCommand = z.infer<
  typeof workstationInitializeCommandSchema
>;
export type WorkstationStartInputCommand = z.infer<
  typeof workstationStartInputCommandSchema
>;
export type WorkstationStopInputCommand = z.infer<
  typeof workstationStopInputCommandSchema
>;
export type WorkstationStartOutputCommand = z.infer<
  typeof workstationStartOutputCommandSchema
>;
export type WorkstationStopOutputCommand = z.infer<
  typeof workstationStopOutputCommandSchema
>;
export type WorkstationOpenOutputCommand = z.infer<
  typeof workstationOpenOutputCommandSchema
>;
export type WorkstationCloseOutputCommand = z.infer<
  typeof workstationCloseOutputCommandSchema
>;
export type WorkstationHomeOutputCommand = z.infer<
  typeof workstationHomeOutputCommandSchema
>;
export type WorkstationCommands = z.infer<
  typeof workstationCommandsSchema
>;
