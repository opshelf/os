import z from 'zod';
import { toolCoordinateSchema } from './movement';

export const dimensionSchema = z.object({
  length: z.number(),
  width: z.number(),
  height: z.number(),
});

export const itemTypeSchema = z.enum(['BOTTLE', 'BOX']);
export const storageModuleSideSchema = z.enum([
  'left_module',
  'right_module',
]);

export const cellStatusSchema = z.enum([
  'OCCUPIED',
  'EMPTY',
  'BLOCKED',
]);

export const gs1Schema = z.object({
  gtin: z.string(),
  expiry: z.string(),
  lot: z.string(),
  sn: z.string(),
  raw_text: z.string(),
});

export const cellItemSchema = z.object({
  dimensions: dimensionSchema,
  item_type: itemTypeSchema.optional().nullable(),
  gs1_data: gs1Schema.optional().nullable(),
  ndc_code: z.string().optional().nullable(),
  weight: z.number().optional().nullable(),
});

export const cellData = z.object({
  status: cellStatusSchema,
  item: cellItemSchema.optional().nullable(),
});

export const traySchema = z.object({
  width: z.number(),
  height: z.number(),
  calibration_point: toolCoordinateSchema.nullable(),
  empty: z.boolean(),
  slots: z.array(cellData),
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

export const storageModuleCellLocationSchema = z.object({
  side: storageModuleSideSchema,
  shelf_idx: z.number().int(),
  tray_idx: z.number().int(),
  slot_idx: z.number().int(),
});

export const storageModuleTrayLocationSchema = z.object({
  side: storageModuleSideSchema,
  shelf_idx: z.number().int(),
  tray_idx: z.number().int(),
});

export const cellLocationDataSchema = z.object({
  cell: cellItemSchema.nullable().optional(),
  location: storageModuleCellLocationSchema,
});

export type Tray = z.infer<typeof traySchema>;
export type ShelfData = z.infer<typeof shelfSchema>;
export type StorageModuleData = z.infer<typeof storageModuleSchema>;
export type StorageModuleSide = keyof StorageModuleData;
export type Dimension = z.infer<typeof dimensionSchema>;
export type ItemType = z.infer<typeof itemTypeSchema>;
export type CellStatus = z.infer<typeof cellStatusSchema>;
export type GS1 = z.infer<typeof gs1Schema>;
export type CellItem = z.infer<typeof cellItemSchema>;
export type CellData = z.infer<typeof cellData>;
export type StorageModuleCellLocation = z.infer<
  typeof storageModuleCellLocationSchema
>;
export type StorageModuleTrayLocationSchema = z.infer<
  typeof storageModuleTrayLocationSchema
>;
export type CellLocationData = z.infer<typeof cellLocationDataSchema>;
