import z from 'zod';
import { gantryCoordinateSchema } from '../movement';
import { storageModuleCellLocationSchema } from '../storage';
import { gantryCommandsSchema } from './gantry';
import { toolCommandsSchema } from './tool';

// Individual command schemas
export const stopCommandSchema = z.object({
  command: z.literal('stop'),
});

export const initializeCommandSchema = z.object({
  command: z.literal('initialize'),
});

export const homeCommandSchema = z.object({
  command: z.literal('home'),
});

export const unsafeMoveCommandSchema = z.object({
  command: z.literal('unsafe_move'),
  coordinate: gantryCoordinateSchema,
});

export const moveCommandSchema = z.object({
  command: z.literal('move'),
  coordinate: gantryCoordinateSchema,
});

export const idleCommandSchema = z.object({
  command: z.literal('idle'),
});

export const grabBoxCommandSchema = z.object({
  command: z.literal('grab_box'),
  coordinate: gantryCoordinateSchema,
  width: z.number(),
});

export const placeBoxCommandSchema = z.object({
  command: z.literal('place_box'),
  coordinate: gantryCoordinateSchema,
  width: z.number(),
});

export const moveToPickupLocationCommandSchema = z.object({
  command: z.literal('move_to_pickup_location'),
});

export const dropOffItemCommandSchema = z.object({
  command: z.literal('drop_off_item'),
});

export const pickupItemCommandSchema = z.object({
  command: z.literal('pickup_item'),
});

export const grabCellCommandSchema = z.object({
  command: z.literal('grab_cell'),
  location: storageModuleCellLocationSchema,
});

export const placeCellCommandSchema = z.object({
  command: z.literal('place_cell'),
  location: storageModuleCellLocationSchema,
});

export const storeItemCommandSchema = z.object({
  command: z.literal('store_item'),
  location: storageModuleCellLocationSchema,
});

export const retrieveItemCommandSchema = z.object({
  command: z.literal('retrieve_item'),
  location: storageModuleCellLocationSchema,
});

export const gantryCommandSchema = z.object({
  command: z.literal('gantry_command'),
  payload: gantryCommandsSchema,
});

export const toolCommandSchema = z.object({
  command: z.literal('tool_command'),
  payload: toolCommandsSchema,
});


// Full RobotCommand discriminated union
export const robotCommandsSchema = z.discriminatedUnion('command', [
  stopCommandSchema,
  initializeCommandSchema,
  homeCommandSchema,
  unsafeMoveCommandSchema,
  moveCommandSchema,
  idleCommandSchema,
  grabBoxCommandSchema,
  placeBoxCommandSchema,
  moveToPickupLocationCommandSchema,
  dropOffItemCommandSchema,
  pickupItemCommandSchema,
  grabCellCommandSchema,
  placeCellCommandSchema,
  storeItemCommandSchema,
  retrieveItemCommandSchema,
  gantryCommandSchema,
  toolCommandSchema
]);

// Type exports
export type RobotCommands = z.infer<typeof robotCommandsSchema>;

// Responses
export const robotCommandSuccessSchema = z.object({
  message: z.literal('success'),
});

export const robotCommandErrorSchema = z.object({
  error: z.string(),
});

export const robotResponseSchema = z.union([
  robotCommandSuccessSchema,
  robotCommandErrorSchema,
]);

export type RobotResponse = z.infer<typeof robotResponseSchema>;
