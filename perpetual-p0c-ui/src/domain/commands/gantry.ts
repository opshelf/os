import { z } from 'zod';
import { baseMessageSchema } from './base';
import { gantryCoordinateSchema } from '../movement';

export const gantryShutdownCommandSchema = baseMessageSchema.extend({
  command: z.literal('shutdown'),
});

export const gantryStopCommandSchema = baseMessageSchema.extend({
  command: z.literal('stop'),
});

export const gantryInitializeCommandSchema = baseMessageSchema.extend(
  {
    command: z.literal('initialize'),
  }
);

export const gantryHomeCommandSchema = baseMessageSchema.extend({
  command: z.literal('home'),
});

export const gantryHomeYFirstCommandSchema = baseMessageSchema.extend(
  {
    command: z.literal('home_y'),
  }
);

export const gantryMoveRelativeCommandSchema =
  baseMessageSchema.extend({
    command: z.literal('move_relative'),
    coordinate: gantryCoordinateSchema,
  });

export const gantryMoveCommandSchema = baseMessageSchema.extend({
  command: z.literal('move'),
  coordinate: gantryCoordinateSchema,
});

// GantryCommands schema
export const gantryCommandsSchema = z.discriminatedUnion('command', [
  gantryShutdownCommandSchema,
  gantryStopCommandSchema,
  gantryInitializeCommandSchema,
  gantryHomeCommandSchema,
  gantryHomeYFirstCommandSchema,
  gantryMoveRelativeCommandSchema,
  gantryMoveCommandSchema,
]);

// Exported types
export type GantryShutdownCommand = z.infer<
  typeof gantryShutdownCommandSchema
>;
export type GantryStopCommand = z.infer<
  typeof gantryStopCommandSchema
>;
export type GantryInitializeCommand = z.infer<
  typeof gantryInitializeCommandSchema
>;
export type GantryHomeCommand = z.infer<
  typeof gantryHomeCommandSchema
>;
export type GantryHomeYFirstCommand = z.infer<
  typeof gantryHomeYFirstCommandSchema
>;
export type GantryMoveRelativeCommand = z.infer<
  typeof gantryMoveRelativeCommandSchema
>;
export type GantryMoveCommand = z.infer<
  typeof gantryMoveCommandSchema
>;
export type GantryCommands = z.infer<typeof gantryCommandsSchema>;
