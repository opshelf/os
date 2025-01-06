import { z } from 'zod';
import { baseMessageSchema } from './base';
import { traySchema } from '../storage';

// Individual command schemas
export const shutdownCommandSchema = baseMessageSchema.extend({
  command: z.literal('shutdown'),
});

export const stopCommandSchema = baseMessageSchema.extend({
  command: z.literal('stop'),
});

export const initializeCommandSchema = baseMessageSchema.extend({
  command: z.literal('initialize'),
});

export const homeCommandSchema = baseMessageSchema.extend({
  command: z.literal('home'),
});

export const stopBeltCommandSchema = baseMessageSchema.extend({
  command: z.literal('stop_belt'),
});

export const startBeltCommandSchema = baseMessageSchema.extend({
  command: z.literal('start_belt'),
});

export const stopWallCommandSchema = baseMessageSchema.extend({
  command: z.literal('stop_wall'),
});

export const startWallCommandSchema = baseMessageSchema.extend({
  command: z.literal('start_wall'),
});

export const stopOutputCommandSchema = baseMessageSchema.extend({
  command: z.literal('stop_output'),
});

export const startOutputCommandSchema = baseMessageSchema.extend({
  command: z.literal('start_output'),
});

export const stopOutputTransferCommandSchema =
  baseMessageSchema.extend({
    command: z.literal('stop_output_transfer'),
  });

export const startOutputTransferCommandSchema =
  baseMessageSchema.extend({
    command: z.literal('start_output_transfer'),
  });

export const moveToCellCommandSchema = baseMessageSchema.extend({
  command: z.literal('move_to_cell'),
  tray: traySchema,
  idx: z.number(),
});

// FunnelCommands schema
export const funnelCommandsSchema = z.discriminatedUnion('command', [
  shutdownCommandSchema,
  stopCommandSchema,
  initializeCommandSchema,
  homeCommandSchema,
  stopBeltCommandSchema,
  startBeltCommandSchema,
  stopWallCommandSchema,
  startWallCommandSchema,
  stopOutputCommandSchema,
  startOutputCommandSchema,
  stopOutputTransferCommandSchema,
  startOutputTransferCommandSchema,
  moveToCellCommandSchema,
]);

// Exported types
export type BaseMessage = z.infer<typeof baseMessageSchema>;
export type ShutdownCommand = z.infer<typeof shutdownCommandSchema>;
export type StopCommand = z.infer<typeof stopCommandSchema>;
export type InitializeCommand = z.infer<
  typeof initializeCommandSchema
>;
export type HomeCommand = z.infer<typeof homeCommandSchema>;
export type StopBeltCommand = z.infer<typeof stopBeltCommandSchema>;
export type StartBeltCommand = z.infer<typeof startBeltCommandSchema>;
export type StopWallCommand = z.infer<typeof stopWallCommandSchema>;
export type StartWallCommand = z.infer<typeof startWallCommandSchema>;
export type StopOutputCommand = z.infer<
  typeof stopOutputCommandSchema
>;
export type StartOutputCommand = z.infer<
  typeof startOutputCommandSchema
>;
export type StopOutputTransferCommand = z.infer<
  typeof stopOutputTransferCommandSchema
>;
export type StartOutputTransferCommand = z.infer<
  typeof startOutputTransferCommandSchema
>;
export type MoveToCellCommand = z.infer<
  typeof moveToCellCommandSchema
>;
export type FunnelCommands = z.infer<typeof funnelCommandsSchema>;
