import z from 'zod';
import { traySchema, storageModuleSideSchema } from '../storage';

export const toolShutdownCommandSchema = z.object({
  command: z.literal('shutdown'),
});

export const toolStopCommandSchema = z.object({
  command: z.literal('stop'),
});

export const toolInitializeCommandSchema = z.object({
  command: z.literal('initialize'),
});

export const toolActuatorsCommandSchema = z.object({
  command: z.literal('actuators'),
  value: z.boolean(),
});

export const toolCarriageSolenoidsCommandSchema = z.object({
  command: z.literal('set_carriage_solenoids'),
  tray: traySchema,
  side: storageModuleSideSchema,
});

export const toolPullerSolenoidsCommandSchema = z.object({
  command: z.literal('set_puller_solenoids'),
  tray: traySchema,
  idx: z.number(),
});

export const toolSolenoidsOffCommandSchema = z.object({
  command: z.literal('solenoids_off'),
});

export const toolSetSlotBitsCommandSchema = z.object({
  command: z.literal('slot_bits'),
  value: z.number(),
});

export const toolExtendCarriageCommandSchema = z.object({
  command: z.literal('extend_carriage'),
  side: storageModuleSideSchema,
});

export const toolRetractCarriageCommandSchema = z.object({
  command: z.literal('retract_carriage'),
  side: storageModuleSideSchema,
});

export const toolRetractTrayCommandSchema = z.object({
  command: z.literal('retract_tray'),
  side: storageModuleSideSchema,
});

export const toolDropItemsCommandSchema = z.object({
  command: z.literal('drop_items'),
  tray: traySchema,
  idx: z.number(),
});

// ToolCommands schema
export const toolCommandsSchema = z.discriminatedUnion('command', [
  toolShutdownCommandSchema,
  toolStopCommandSchema,
  toolInitializeCommandSchema,
  toolActuatorsCommandSchema,
  toolCarriageSolenoidsCommandSchema,
  toolPullerSolenoidsCommandSchema,
  toolSolenoidsOffCommandSchema,
  toolSetSlotBitsCommandSchema,
  toolExtendCarriageCommandSchema,
  toolRetractCarriageCommandSchema,
  toolRetractTrayCommandSchema,
  toolDropItemsCommandSchema,
]);

// Exported types
export type ToolShutdownCommand = z.infer<
  typeof toolShutdownCommandSchema
>;
export type ToolStopCommand = z.infer<typeof toolStopCommandSchema>;
export type ToolInitializeCommand = z.infer<
  typeof toolInitializeCommandSchema
>;
export type ToolActuatorsCommand = z.infer<
  typeof toolActuatorsCommandSchema
>;
export type ToolCarriageSolenoidsCommand = z.infer<
  typeof toolCarriageSolenoidsCommandSchema
>;
export type ToolPullerSolenoidsCommand = z.infer<
  typeof toolPullerSolenoidsCommandSchema
>;
export type ToolSolenoidsOffCommand = z.infer<
  typeof toolSolenoidsOffCommandSchema
>;
export type ToolSetSlotBitsCommand = z.infer<
  typeof toolSetSlotBitsCommandSchema
>;
export type ToolExtendCarriageCommand = z.infer<
  typeof toolExtendCarriageCommandSchema
>;
export type ToolRetractCarriageCommand = z.infer<
  typeof toolRetractCarriageCommandSchema
>;
export type ToolRetractTrayCommand = z.infer<
  typeof toolRetractTrayCommandSchema
>;
export type ToolDropItemsCommand = z.infer<
  typeof toolDropItemsCommandSchema
>;
export type ToolCommands = z.infer<typeof toolCommandsSchema>;
