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
  sendFunnelCommand: (funnelCommand: FunnelCommands) => Promise<void>;
  constructor(
    sendFunnelCommand: (
      funnelCommand: FunnelCommands
    ) => Promise<void>
  ) {
    this.sendFunnelCommand = sendFunnelCommand;
  }

  async shutdownCommand(): Promise<void> {
    await this.sendFunnelCommand({ command: 'shutdown' });
  }
  async stopCommand(): Promise<void> {
    await this.sendFunnelCommand({ command: 'stop' });
  }
  async initializeCommand(): Promise<void> {
    await this.sendFunnelCommand({ command: 'initialize' });
  }
  async homeCommand(): Promise<void> {
    await this.sendFunnelCommand({ command: 'home' });
  }
  async stopBeltCommand(): Promise<void> {
    await this.sendFunnelCommand({ command: 'stop_belt' });
  }
  async startBeltCommand(): Promise<void> {
    await this.sendFunnelCommand({ command: 'start_belt' });
  }
  async stopWallCommand(): Promise<void> {
    await this.sendFunnelCommand({ command: 'stop_wall' });
  }
  async startWallCommand(): Promise<void> {
    await this.sendFunnelCommand({ command: 'start_wall' });
  }
  async stopOutputCommand(): Promise<void> {
    await this.sendFunnelCommand({ command: 'stop_output' });
  }
  async startOutputCommand(): Promise<void> {
    await this.sendFunnelCommand({ command: 'start_output' });
  }
  async stopOutputTransferCommand(): Promise<void> {
    await this.sendFunnelCommand({ command: 'stop_output_transfer' });
  }
  async startOutputTransferCommand(): Promise<void> {
    await this.sendFunnelCommand({
      command: 'start_output_transfer',
    });
  }
  async moveToCellCommand(tray: Tray, idx: number): Promise<void> {
    await this.sendFunnelCommand({
      command: 'move_to_cell',
      tray,
      idx,
    });
  }
}
