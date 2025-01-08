import z from 'zod';
import { gantryStatus } from './gantry';

export const subsystemStatus = z.object({
  gantry: gantryStatus.nullable(),
  //tool:
  //io:
  //funnel:
});

export type SubsystemStatus = z.infer<typeof subsystemStatus>;