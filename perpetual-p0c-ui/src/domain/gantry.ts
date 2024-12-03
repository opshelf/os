import { GantryCoordinateSchema } from './movement';
import z from 'zod';

export const gantryCommands = z.enum([
  'HOME',
  'INITIALIZE',
  'MOVE',
  'MOVE_RELATIVE',
  'STOP',
]);

export type GantryCommands = z.infer<typeof gantryCommands>;

export type GantrySubsystemCommands = {
  command: GantryCommands;
  payload: any;
};

export type GantryMoveCommands = GantrySubsystemCommands & {
  command: 'MOVE' | 'MOVE_RELATIVE';
  payload: GantryCoordinateSchema;
};
