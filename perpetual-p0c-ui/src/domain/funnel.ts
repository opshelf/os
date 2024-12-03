import z from 'zod';

export type TrayConfiguration = {
  cells: number;
  cell_depth: number;
  cell_width: number;
};
/*
class Commands(str, Enum):
    HOME = "HOME"
    STOP = "STOP"
    MOVE_TO_CELL = "MOVE_TO_CELL"
    CLOSE_FUNNEL = "CLOSE_FUNNEL"
    INLET_COMMAND = "INLET_COMMAND"
    OPPOSING_FUNNEL_COMMAND = "OPPOSING_FUNNEL_COMMAND"
    START_INLET_BELT = "START_INLET_BELT"
    STOP_INLET_BELT = "STOP_INLET_BELT"
    START_TRANSFER_BELT = "START_TRANSFER_BELT"
    STOP_TRANSFER_BELT = "STOP_TRANSFER_BELT"
    START_OUTLET_BELT = "START_OUTLET_BELT"
    STOP_OUTLET_BELT = "STOP_OUTLET_BELT"
    WIGGLE = "WIGGLE"
    INITIALIZE = "INITIALZE"
*/
const funnelSubsytemCommands = z.enum([
  'HOME',
  'STOP',
  'MOVE_TO_CELL',
  'CLOSE_FUNNEL',
  'INLET_COMMAND',
  'OPPOSING_FUNNEL_COMMAND',
  'START_INLET_BELT',
  'STOP_INLET_BELT',
  'START_TRANSFER_BELT',
  'STOP_TRANSFER_BELT',
  'START_OUTLET_BELT',
  'STOP_OUTLET_BELT',
  'START_WALL_BELT',
  'STOP_WALL_BELT',
  'WIGGLE',
  'INITIALZE',
]);

export type FunnelSubsytemCommands = z.infer<
  typeof funnelSubsytemCommands
>;

const funnelCommands = z.object({
  command: funnelSubsytemCommands,
  payload: z.any(),
});

export type FunnelCommands = z.infer<typeof funnelCommands>;

export type FunnelBeltCommands = FunnelCommands & {
  command:
    | 'START_INLET_BELT'
    | 'START_OUTLET_BELT'
    | 'START_TRANSFER_BELT'
    | 'START_WALL_BELT';
  payload: { speed: number } | null;
};
