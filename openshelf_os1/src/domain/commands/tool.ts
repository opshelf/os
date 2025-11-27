import z from 'zod';

// Command Schemas
export const toolShutdownCommandSchema = z.object({
  command: z.literal('shutdown'),
});

export const toolStopCommandSchema = z.object({
  command: z.literal('stop'),
});

export const toolInitializeCommandSchema = z.object({
  command: z.literal('initialize'),
});

export const toolExtendGripperCommandSchema = z.object({
  command: z.literal('extend_gripper'),
});

export const toolRetractGripperCommandSchema = z.object({
  command: z.literal('retract_gripper'),
});

export const toolHomeCommandSchema = z.object({
  command: z.literal('home'),
});

export const toolGripperMoveAbsoluteSchema = z.object({
  command: z.literal('move'),
  position: z.number(),
});

export const toolGripperMoveRelativeSchema = z.object({
  command: z.literal('move_relative'),
  position: z.number(),
});

export const toolGripperJawSizeSchema = z.object({
  command: z.literal('set_jaw_size'),
  size: z.number(),
});

export const toolGripperOpenSchema = z.object({
  command: z.literal('open_gripper'),
});

export const toolGripperCloseSchema = z.object({
  command: z.literal('close_gripper'),
});

export const toolGripperPrepareSchema = z.object({
  command: z.literal('prepare_gripper'),
});

// ToolCommands schema
export const toolCommandsSchema = z.discriminatedUnion('command', [
  toolShutdownCommandSchema,
  toolStopCommandSchema,
  toolInitializeCommandSchema,
  toolHomeCommandSchema,
  toolExtendGripperCommandSchema,
  toolRetractGripperCommandSchema,
  toolGripperMoveAbsoluteSchema,
  toolGripperMoveRelativeSchema,
  toolGripperJawSizeSchema,
  toolGripperOpenSchema,
  toolGripperCloseSchema,
  toolGripperPrepareSchema,
]);

// Exported types
export type ToolShutdownCommand = z.infer<
  typeof toolShutdownCommandSchema
>;
export type ToolStopCommand = z.infer<typeof toolStopCommandSchema>;
export type ToolInitializeCommand = z.infer<
  typeof toolInitializeCommandSchema
>;
export type ToolHomeCommand = z.infer<typeof toolHomeCommandSchema>;
export type ToolExtendGripperCommand = z.infer<
  typeof toolExtendGripperCommandSchema
>;
export type ToolRetractGripperCommand = z.infer<
  typeof toolRetractGripperCommandSchema
>;
export type ToolGripperMoveAbsoluteCommand = z.infer<
  typeof toolGripperMoveAbsoluteSchema
>;
export type ToolGripperMoveRelativeCommand = z.infer<
  typeof toolGripperMoveRelativeSchema
>;
export type ToolGripperJawSizeCommand = z.infer<
  typeof toolGripperJawSizeSchema
>;
export type ToolGripperOpenCommand = z.infer<
  typeof toolGripperOpenSchema
>;
export type ToolGripperCloseCommand = z.infer<
  typeof toolGripperCloseSchema
>;
export type ToolGripperPrepareCommand = z.infer<
  typeof toolGripperPrepareSchema
>;

export type ToolCommands = z.infer<typeof toolCommandsSchema>;

export class Tool {
  sendToolCommand: (toolCommand: ToolCommands) => Promise<void>;

  constructor(
    sendToolCommand: (toolCommand: ToolCommands) => Promise<void>
  ) {
    this.sendToolCommand = sendToolCommand;
  }

  async toolShutdownCommand(): Promise<void> {
    await this.sendToolCommand({ command: 'shutdown' });
  }

  async toolStopCommand(): Promise<void> {
    await this.sendToolCommand({ command: 'stop' });
  }

  async toolInitializeCommand(): Promise<void> {
    await this.sendToolCommand({ command: 'initialize' });
  }

  async toolHomeCommand(): Promise<void> {
    await this.sendToolCommand({
      command: 'home'
    });
  }

  async toolExtendGripperCommand(): Promise<void> {
    await this.sendToolCommand({ command: 'extend_gripper' });
  }

  async toolRetractGripperCommand(): Promise<void> {
    await this.sendToolCommand({ command: 'retract_gripper' });
  }

  async toolMoveGripperAbsolute(position: number): Promise<void> {
    await this.sendToolCommand({ command: 'move', position });
  }

  async toolMoveGripperRelative(position: number): Promise<void> {
    await this.sendToolCommand({
      command: 'move_relative',
      position,
    });
  }

  async toolSetJawSize(size: number): Promise<void> {
    await this.sendToolCommand({ command: 'set_jaw_size', size });
  }

  async toolOpenGripper(): Promise<void> {
    await this.sendToolCommand({ command: 'open_gripper' });
  }

  async toolCloseGripper(): Promise<void> {
    await this.sendToolCommand({
      command: 'close_gripper',
    });
  }

  async toolPrepareGripper(): Promise<void> {
    await this.sendToolCommand({
      command: 'prepare_gripper',
    });
  }
}
