import z from 'zod';
import { toolCoordinateSchema } from './movement';

// Enums
export const storageModuleSideSchema = z.enum([
  'left_module',
  'right_module',
]);
export const cellStatusSchema = z.enum([
  'OCCUPIED',
  'EMPTY',
  'BLOCKED',
]);

// Shared dimension model
export const dimensionSchema = z.object({
  length: z.number(),
  width: z.number(),
  height: z.number(),
});

// Cell item (only contains dimensions and optional weight)
export const cellItemSchema = z.object({
  dimensions: dimensionSchema,
  weight: z.number().optional(),
});

// Cell schema
export const cellSchema = z.object({
  status: cellStatusSchema,
  item: cellItemSchema.optional().nullable(),
  calibration_point: toolCoordinateSchema.nullable(),
  dimensions: dimensionSchema,
  empty: z.boolean(),
});

// Shelf schema
export const shelfSchema = z.object({
  shelf_height: z.number(),
  shelf_width: z.number(),
  cells: z.array(cellSchema),
  divider_width: z.number().default(8.0), // Matching default in Python
});

// Entire storage module
export const storageModuleSchema = z.object({
  left_module: z.array(shelfSchema),
  right_module: z.array(shelfSchema),
});

// Cell location in a storage module
export const storageModuleCellLocationSchema = z.object({
  side: storageModuleSideSchema,
  shelf_idx: z.number().int(),
  cell_idx: z.number().int(),
});

// Location and contents
export const cellLocationDataSchema = z.object({
  cell: cellItemSchema.nullable().optional(),
  location: storageModuleCellLocationSchema,
});

// Type exports
export type StorageModuleSide = z.infer<
  typeof storageModuleSideSchema
>;
export type CellStatus = z.infer<typeof cellStatusSchema>;
export type Dimension = z.infer<typeof dimensionSchema>;
export type CellItem = z.infer<typeof cellItemSchema>;
export type Cell = z.infer<typeof cellSchema>;
export type ShelfData = z.infer<typeof shelfSchema>;
export type StorageModuleData = z.infer<typeof storageModuleSchema>;
export type StorageModuleCellLocation = z.infer<
  typeof storageModuleCellLocationSchema
>;
export type CellLocationData = z.infer<typeof cellLocationDataSchema>;
