import { z } from "zod";
import {
  storageModuleCellLocationSchema,
  cellItemSchema,
  storageModuleTrayLocationSchema,
} from "../storage";
import { baseMessageSchema } from "./base";
import { gantryCommandsSchema } from "./gantry";
import { toolCommandsSchema } from "./tool";

const storePayloadSchema = z.object({
  location: storageModuleCellLocationSchema,
  item: cellItemSchema.optional().nullable(),
});

export const storeCommandSchema = baseMessageSchema.extend({
  command: z.literal("store_item"),
  payload: storePayloadSchema,
});

const retrievePayloadSchema = z.object({
  location: storageModuleCellLocationSchema,
  item: cellItemSchema.optional().nullable(),
});

export const retrieveCommandSchema = baseMessageSchema.extend({
  command: z.literal("retrieve_item"),
  payload: retrievePayloadSchema,
});

export const homeCommandSchema = baseMessageSchema.extend({
  command: z.literal("home"),
});

export const homeYFirstCommandSchema = baseMessageSchema.extend({
  command: z.literal("home_y_first"),
});

export const stopCommandSchema = baseMessageSchema.extend({
  command: z.literal("stop"),
});

export const pauseCommandSchema = baseMessageSchema.extend({
  command: z.literal("pause"),
});

export const initializeCommandSchema = baseMessageSchema.extend({
  command: z.literal("initialize"),
});

export const returnCurrentTraySchema = baseMessageSchema.extend({
  command: z.literal("return_current_tray"),
});

export const idlePositionCommandSchema = baseMessageSchema.extend({
  command: z.literal("idle_position"),
});

export const serviceHighPositionCommandSchema = baseMessageSchema.extend({
  command: z.literal("service_high_position"),
});

export const pullTrayCommandSchema = baseMessageSchema.extend({
  command: z.literal("pull_tray"),
  payload: storageModuleTrayLocationSchema,
});

export const placeTrayCommandSchema = baseMessageSchema.extend({
  command: z.literal("place_tray"),
  payload: z.union([
    storageModuleCellLocationSchema,
    storageModuleTrayLocationSchema,
  ]),
});

export const toolSubsystemCommandSchema = baseMessageSchema.extend({
  command: z.literal("tool_command"),
  payload: toolCommandsSchema,
});

export const gantrySubsystemCommandsSchema = baseMessageSchema.extend({
  command: z.literal("gantry_command"),
  payload: gantryCommandsSchema,
});

export const axisHomeCommandSchema = z.object({
  command: z.literal("home"),
});
export type AxisHomeCommand = z.infer<typeof axisHomeCommandSchema>;

export const axisInitializeCommandSchema = z.object({
  command: z.literal("initialize"),
});
export type AxisInitializeCommand = z.infer<typeof axisInitializeCommandSchema>;

export const axisMoveRelativeCommandSchema = z.object({
  command: z.literal("move_relative"),
  position: z.number(),
});
export type AxisMoveRelativeCommand = z.infer<
  typeof axisMoveRelativeCommandSchema
>;

export const axisMoveAbsoluteCommandSchema = z.object({
  command: z.literal("move_absolute"),
  position: z.number(),
});
export type AxisMoveAbsoluteCommand = z.infer<
  typeof axisMoveAbsoluteCommandSchema
>;

export const axisStopCommandSchema = z.object({
  command: z.literal("stop"),
});
export type AxisStopCommand = z.infer<typeof axisStopCommandSchema>;

export const axisCommandsSchema = z.discriminatedUnion("command", [
  axisHomeCommandSchema,
  axisInitializeCommandSchema,
  axisMoveRelativeCommandSchema,
  axisMoveAbsoluteCommandSchema,
  axisStopCommandSchema,
]);
export type AxisCommands = z.infer<typeof axisCommandsSchema>;

export const axisCommandPayloadSchema = z.object({
  axis_id: z.number(),
  command: axisCommandsSchema,
});
export type AxisCommandPayload = z.infer<typeof axisCommandPayloadSchema>;

export const lowLevelAxisCommandsSchema = baseMessageSchema.extend({
  command: z.literal("axis_command"),
  payload: axisCommandPayloadSchema,
});
export type LowLevelAxisCommands = z.infer<typeof lowLevelAxisCommandsSchema>;

export const robotCommandsSchema = z.discriminatedUnion("command", [
  storeCommandSchema,
  retrieveCommandSchema,
  homeCommandSchema,
  homeYFirstCommandSchema,
  stopCommandSchema,
  pauseCommandSchema,
  initializeCommandSchema,
  returnCurrentTraySchema,
  pullTrayCommandSchema,
  placeTrayCommandSchema,
  idlePositionCommandSchema,
  serviceHighPositionCommandSchema,
  lowLevelAxisCommandsSchema,
  // Subsystem Commands
  toolSubsystemCommandSchema,
  gantrySubsystemCommandsSchema,
]);

export type StoreCommand = z.infer<typeof storeCommandSchema>;
export type RetrieveCommand = z.infer<typeof retrieveCommandSchema>;
export type HomeCommand = z.infer<typeof homeCommandSchema>;
export type HomeYFirstCommand = z.infer<typeof homeYFirstCommandSchema>;
export type StopCommand = z.infer<typeof stopCommandSchema>;
export type PauseCommand = z.infer<typeof pauseCommandSchema>;
export type InitializeCommand = z.infer<typeof initializeCommandSchema>;
export type ReturnCurrentTray = z.infer<typeof returnCurrentTraySchema>;
export type PullTrayCommand = z.infer<typeof pullTrayCommandSchema>;
export type PlaceTrayCommand = z.infer<typeof placeTrayCommandSchema>;

export type IdlePositionCommand = z.infer<typeof idlePositionCommandSchema>;
export type ServiceHighPositionCommand = z.infer<
  typeof serviceHighPositionCommandSchema
>;
export type ToolSubsystemCommand = z.infer<typeof toolSubsystemCommandSchema>;
export type GantrySubsystemCommands = z.infer<
  typeof gantrySubsystemCommandsSchema
>;
export type RobotCommands = z.infer<typeof robotCommandsSchema>;
