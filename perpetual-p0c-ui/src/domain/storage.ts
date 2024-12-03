import z from 'zod';
import { toolCoordinateSchema } from './movement';

export const traySchema = z.object({
  width: z.number(),
  height: z.number(),
  calibration_point: toolCoordinateSchema.optional().nullable(),
});

export const shelfSchema = z.object({
  shelf_height: z.number(),
  shelf_width: z.number(),
  trays: z.array(traySchema),
});

export const storageModuleSchema = z.object({
  left_module: z.array(shelfSchema),
  right_module: z.array(shelfSchema),
});

//export type Tray = {
//  width: number;
//  height: number;
//  calibration_point: number | null;
//};

//export type ShelfData = {
//  shelf_height: number;
//  shelf_width: number;
//  trays: Tray[];
//};

//export type StorageModuleData = {
//  left_module: ShelfData[];
//  right_module: ShelfData[];
//};

export type Tray = z.infer<typeof traySchema>;
export type ShelfData = z.infer<typeof shelfSchema>;
export type StorageModuleData = z.infer<typeof storageModuleSchema>;
export type StorageModuleSide = keyof StorageModuleData;
