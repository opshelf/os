import { GantryCoordinateSchema } from "./movement";
import z from "zod";

export const gantryCommands = z.enum([
  "HOME",
  "INITIALIZE",
  "MOVE",
  "MOVE_RELATIVE",
  "STOP",
]);

export type GantryCommands = z.infer<typeof gantryCommands>;

export type GantrySubsystemCommands = {
  command: GantryCommands;
  payload: any;
};

export const gantryStatus = z.object({
  x_position: z.number(),
  y_position: z.number(),
});

export type GantryStatus = z.infer<typeof gantryStatus>;

export type GantryMoveCommands = GantrySubsystemCommands & {
  command: "MOVE" | "MOVE_RELATIVE";
  payload: GantryCoordinateSchema;
};
