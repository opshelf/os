import { z } from 'zod';
import { Tray, traySchema } from '../storage';

// Individual command schemas
export const shutdownCommandSchema = z.object({
  command: z.literal('shutdown'),
});

export const stopCommandSchema = z.object({
  command: z.literal('stop'),
});

export const initializeCommandSchema = z.object({
  command: z.literal('initialize'),
});

export const homeCommandSchema = z.object({
  command: z.literal('home'),
});

export const stopBeltCommandSchema = z.object({
  command: z.literal('stop_belt'),
});

export const startBeltCommandSchema = z.object({
  command: z.literal('start_belt'),
});

export const stopWallCommandSchema = z.object({
  command: z.literal('stop_wall'),
});

export const startWallCommandSchema = z.object({
  command: z.literal('start_wall'),
});

export const stopOutputCommandSchema = z.object({
  command: z.literal('stop_output'),
});

export const startOutputCommandSchema = z.object({
  command: z.literal('start_output'),
});

export const stopOutputTransferCommandSchema = z.object({
  command: z.literal('stop_output_transfer'),
});

export const startOutputTransferCommandSchema = z.object({
  command: z.literal('start_output_transfer'),
});

export const moveToCellCommandSchema = z.object({
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

export class Funnel {
  shutdownCommand(): ShutdownCommand {
    return { command: 'shutdown' };
  }
  stopCommand(): StopCommand {
    return { command: 'stop' };
  }
  initializeCommand(): InitializeCommand {
    return { command: 'initialize' };
  }
  homeCommand(): HomeCommand {
    return { command: 'home' };
  }
  stopBeltCommand(): StopBeltCommand {
    return { command: 'stop_belt' };
  }
  startBeltCommand(): StartBeltCommand {
    return { command: 'start_belt' };
  }
  stopWallCommand(): StopWallCommand {
    return { command: 'stop_wall' };
  }
  startWallCommand(): StartWallCommand {
    return { command: 'start_wall' };
  }
  stopOutputCommand(): StopOutputCommand {
    return { command: 'stop_output' };
  }
  startOutputCommand(): StartOutputCommand {
    return { command: 'start_output' };
  }
  stopOutputTransferCommand(): StopOutputTransferCommand {
    return { command: 'stop_output_transfer' };
  }
  startOutputTransferCommand(): StartOutputTransferCommand {
    return { command: 'start_output_transfer' };
  }
  moveToCellCommand(tray: Tray, idx: number): MoveToCellCommand {
    return { command: 'move_to_cell', tray, idx };
  }
}
