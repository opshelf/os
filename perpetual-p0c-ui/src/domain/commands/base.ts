import { z } from 'zod';

// UUID Type (as string)
export const UUID4 = z.string().uuid();
export const baseMessageSchema = z.object({
  message_id: UUID4,
});
export type BaseMessageSchema = z.infer<typeof baseMessageSchema>;
