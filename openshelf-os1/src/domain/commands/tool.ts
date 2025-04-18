/*
class ToolStatus(BaseModel):
    gripper: AxisStatus
    z_axis: AxisStatus


class StopCommand(BaseModel):
    command: Literal["stop"]


class InitializeCommand(BaseModel):
    command: Literal["initialize"]


class HomeCommand(BaseModel):
    command: Literal["home"]


class MoveCommand(BaseModel):
    command: Literal["move"]
    position: float


class MoveRelativeCommand(BaseModel):
    command: Literal["move_relative"]
    position: float


class SetJawSizeCommand(BaseModel):
    command: Literal["set_jaw_size"]
    size: float


ToolCommands = Annotated[
    Union[
        StopCommand,
        InitializeCommand,
        HomeCommand,
        MoveCommand,
        MoveRelativeCommand,
        SetJawSizeCommand,
    ],
    Field(discriminator="command"),
]
*/

import z from "zod";
import {
  traySchema,
  storageModuleSideSchema,
  StorageModuleSide,
  Tray,
} from "../storage";

export const toolShutdownCommandSchema = z.object({
  command: z.literal("shutdown"),
});

export const toolStopCommandSchema = z.object({
  command: z.literal("stop"),
});

export const toolInitializeCommandSchema = z.object({
  command: z.literal("initialize"),
});

export const toolExtendCarriageCommandSchema = z.object({
  command: z.literal("extend_carriage"),
  side: storageModuleSideSchema,
});

export const toolRetractCarriageCommandSchema = z.object({
  command: z.literal("retract_carriage"),
  side: storageModuleSideSchema,
});

export const toolRetractTrayCommandSchema = z.object({
  command: z.literal("retract_tray"),
  side: storageModuleSideSchema,
});

export const toolDropItemsCommandSchema = z.object({
  command: z.literal("drop_items"),
  tray: traySchema,
  idx: z.number(),
});

export const toolHomeCommandSchema = z.object({
  command: z.literal("home"),
});

export const toolCarriageMoveAbsoluteSchema = z.object({
  command: z.literal("carriage_absolute"),
  position: z.number(),
});

export const toolCarriageMoveRelativeSchema = z.object({
  command: z.literal("carriage_relative"),
  position: z.number(),
});

// ToolCommands schema
export const toolCommandsSchema = z.discriminatedUnion("command", [
  toolShutdownCommandSchema,
  toolStopCommandSchema,
  toolInitializeCommandSchema,
  toolExtendCarriageCommandSchema,
  toolRetractCarriageCommandSchema,
  toolRetractTrayCommandSchema,
  toolDropItemsCommandSchema,
  toolHomeCommandSchema,
  toolCarriageMoveAbsoluteSchema,
  toolCarriageMoveRelativeSchema,
]);

// Exported types
export type ToolShutdownCommand = z.infer<typeof toolShutdownCommandSchema>;
export type ToolStopCommand = z.infer<typeof toolStopCommandSchema>;
export type ToolInitializeCommand = z.infer<typeof toolInitializeCommandSchema>;
export type ToolExtendCarriageCommand = z.infer<
  typeof toolExtendCarriageCommandSchema
>;
export type ToolRetractCarriageCommand = z.infer<
  typeof toolRetractCarriageCommandSchema
>;
export type ToolRetractTrayCommand = z.infer<
  typeof toolRetractTrayCommandSchema
>;
export type ToolDropItemsCommand = z.infer<typeof toolDropItemsCommandSchema>;

export type ToolHomeCommand = z.infer<typeof toolHomeCommandSchema>;
export type ToolCarriageMoveAbsolute = z.infer<
  typeof toolCarriageMoveAbsoluteSchema
>;
export type ToolCarriageMoveRelative = z.infer<
  typeof toolCarriageMoveRelativeSchema
>;

export type ToolCommands = z.infer<typeof toolCommandsSchema>;

export class Tool {
  sendToolCommand: (toolCommand: ToolCommands) => Promise<void>;

  constructor(sendToolCommand: (toolCommand: ToolCommands) => Promise<void>) {
    this.sendToolCommand = sendToolCommand;
  }

  async toolShutdownCommand(): Promise<void> {
    await this.sendToolCommand({ command: "shutdown" });
  }

  async toolStopCommand(): Promise<void> {
    await this.sendToolCommand({ command: "stop" });
  }

  async toolInitializeCommand(): Promise<void> {
    await this.sendToolCommand({ command: "initialize" });
  }

  async toolExtendCarriageCommand(side: StorageModuleSide): Promise<void> {
    await this.sendToolCommand({ command: "extend_carriage", side });
  }

  async toolRetractCarriageCommand(side: StorageModuleSide): Promise<void> {
    await this.sendToolCommand({ command: "retract_carriage", side });
  }

  async toolRetractTrayCommand(side: StorageModuleSide): Promise<void> {
    await this.sendToolCommand({ command: "retract_tray", side });
  }

  async toolDropItemsCommand(tray: Tray, idx: number): Promise<void> {
    await this.sendToolCommand({ command: "drop_items", tray, idx });
  }

  async toolHomeCommand(): Promise<void> {
    await this.sendToolCommand({ command: "home" });
  }
  async toolCarriageMoveAbsolute(position: number): Promise<void> {
    await this.sendToolCommand({
      command: "carriage_absolute",
      position,
    });
  }
  async toolCarriageMoveRelative(position: number): Promise<void> {
    await this.sendToolCommand({
      command: "carriage_relative",
      position,
    });
  }
}
