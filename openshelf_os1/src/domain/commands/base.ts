import { z } from "zod";

export const UUID4 = z.string().uuid();
export const baseMessageSchema = z.object({
  message_id: UUID4,
});
export type BaseMessageSchema = z.infer<typeof baseMessageSchema>;
