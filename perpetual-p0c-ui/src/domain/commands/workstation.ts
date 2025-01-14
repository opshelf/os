import z from 'zod';

export const workstationShutdownCommandSchema = z.object({
  command: z.literal('shutdown'),
});

export const workstationStopCommandSchema = z.object({
  command: z.literal('stop'),
});

export const workstationInitializeCommandSchema = z.object({
  command: z.literal('initialize'),
});

export const workstationStartInputCommandSchema = z.object({
  command: z.literal('start_input_belt'),
});

export const workstationStopInputCommandSchema = z.object({
  command: z.literal('stop_input_belt'),
});

export const workstationStartOutputCommandSchema = z.object({
  command: z.literal('start_output_belt'),
});

export const workstationStopOutputCommandSchema = z.object({
  command: z.literal('stop_output_belt'),
});

export const workstationOpenOutputCommandSchema = z.object({
  command: z.literal('open_output'),
});

export const workstationCloseOutputCommandSchema = z.object({
  command: z.literal('close_output'),
});

export const workstationHomeOutputCommandSchema = z.object({
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

export class Workstation {
  sendWorkstationCommand: (
    workstationCommand: WorkstationCommands
  ) => Promise<void>;

  constructor(
    sendWorkstationCommand: (
      workstationCommand: WorkstationCommands
    ) => Promise<void>
  ) {
    this.sendWorkstationCommand = sendWorkstationCommand;
  }

  async workstationShutdownCommand(): Promise<void> {
    await this.sendWorkstationCommand({ command: 'shutdown' });
  }

  async workstationStopCommand(): Promise<void> {
    await this.sendWorkstationCommand({ command: 'stop' });
  }

  async workstationInitializeCommand(): Promise<void> {
    await this.sendWorkstationCommand({ command: 'initialize' });
  }

  async workstationStartInputCommand(): Promise<void> {
    await this.sendWorkstationCommand({
      command: 'start_input_belt',
    });
  }

  async workstationStopInputCommand(): Promise<void> {
    await this.sendWorkstationCommand({ command: 'stop_input_belt' });
  }

  async workstationStartOutputCommand(): Promise<void> {
    await this.sendWorkstationCommand({
      command: 'start_output_belt',
    });
  }

  async workstationStopOutputCommand(): Promise<void> {
    await this.sendWorkstationCommand({
      command: 'stop_output_belt',
    });
  }

  async workstationOpenOutputCommand(): Promise<void> {
    await this.sendWorkstationCommand({ command: 'open_output' });
  }

  async workstationCloseOutputCommand(): Promise<void> {
    await this.sendWorkstationCommand({ command: 'close_output' });
  }

  async workstationHomeOutputCommand(): Promise<void> {
    await this.sendWorkstationCommand({ command: 'home_output' });
  }
}
