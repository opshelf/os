import z from 'zod';

const toolSubsytemCommands = z.enum([
  'HOME',
  'INITIALIZE',
  'MOVE_Z',
  'MOVE_Z_RELATIVE',
  'TURN_ON_PULLER_SOLENOIDS',
  'TURN_OFF_PULLER_SOLENOIDS',
  'TURN_ON_SLOT_SOLENOIDS',
  'TURN_OFF_SLOT_SOLENOIDS',
  'RETRACT_TRAY',
  'EXTEND_PULLER',
  'RETRACT_PULLER',
  'EXTEND_ACTUATORS',
  'RETRACT_ACTUATORS',
]);

export type ToolSubsytemCommands = z.infer<
  typeof toolSubsytemCommands
>;

const toolCommands = z.object({
  command: toolSubsytemCommands,
  payload: z.any(),
});

export type ToolCommands = z.infer<typeof toolCommands>;
