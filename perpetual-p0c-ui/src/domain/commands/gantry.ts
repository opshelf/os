import { z } from 'zod';
import {
  GantryCoordinateSchema,
  gantryCoordinateSchema,
} from '../movement';

export const gantryShutdownCommandSchema = z.object({
  command: z.literal('shutdown'),
});

export const gantryStopCommandSchema = z.object({
  command: z.literal('stop'),
});

export const gantryInitializeCommandSchema = z.object({
  command: z.literal('initialize'),
});

export const gantryHomeCommandSchema = z.object({
  command: z.literal('home'),
});

export const gantryHomeYFirstCommandSchema = z.object({
  command: z.literal('home_y'),
});

export const gantryMoveRelativeCommandSchema = z.object({
  command: z.literal('move_relative'),
  coordinate: gantryCoordinateSchema,
});

export const gantryMoveCommandSchema = z.object({
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

export class Gantry {
  sendGantryCommand: (gantryCommand: GantryCommands) => Promise<void>;
  constructor(
    sendGantryCommand: (
      gantryCommand: GantryCommands
    ) => Promise<void>
  ) {
    this.sendGantryCommand = sendGantryCommand;
  }
  /*
  gantryShutdownCommand(): GantryShutdownCommand {
    return { command: 'shutdown' };
  }
  gantryStopCommand(): GantryStopCommand {
    return { command: 'stop' };
  }
  gantryInitializeCommand(): GantryInitializeCommand {
    return { command: 'initialize' };
  }
  gantryHomeCommand(): GantryHomeCommand {
    return { command: 'home' };
  }
  gantryHomeYFirstCommand(): GantryHomeYFirstCommand {
    return { command: 'home_y' };
  }
  gantryMoveCommand(
    coordinate: GantryCoordinateSchema
  ): GantryMoveCommand {
    return { command: 'move', coordinate };
  }
  gantryMoveRelativeCommand(
    coordinate: GantryCoordinateSchema
  ): GantryMoveRelativeCommand {
    return { command: 'move_relative', coordinate };
  }
  */
  async gantryShutdownCommand(): Promise<void> {
    await this.sendGantryCommand({ command: 'shutdown' });
  }
  async gantryStopCommand(): Promise<void> {
    await this.sendGantryCommand({ command: 'stop' });
  }
  async gantryInitializeCommand(): Promise<void> {
    await this.sendGantryCommand({ command: 'initialize' });
  }
  async gantryHomeCommand(): Promise<void> {
    await this.sendGantryCommand({ command: 'home' });
  }
  async gantryHomeYFirstCommand(): Promise<void> {
    await this.sendGantryCommand({ command: 'home_y' });
  }
  async gantryMoveCommand(
    coordinate: GantryCoordinateSchema
  ): Promise<void> {
    await this.sendGantryCommand({ command: 'move', coordinate });
  }
  async gantryMoveRelativeCommand(
    coordinate: GantryCoordinateSchema
  ): Promise<void> {
    await this.sendGantryCommand({
      command: 'move_relative',
      coordinate,
    });
  }
}
