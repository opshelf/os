import z from "zod";

export const gantryCoordinateSchema = z.object({
  x: z.number(),
  y: z.number(),
});

export const toolCoordinateSchema = gantryCoordinateSchema.extend({
  z: z.number(),
});

export type GantryCoordinateSchema = z.infer<typeof gantryCoordinateSchema>;

export type ToolCoordinateSchema = z.infer<typeof toolCoordinateSchema>;
