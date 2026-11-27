import { z } from 'zod';
import { TaskInputTextSchema } from './models.js';

const ReplayContextSchema = z.object({
  version: z.literal(1),
  feedback: z.string(),
  messages: z.array(z.object({
    type: z.string(),
    text: z.unknown().optional(),
    finish: z.unknown().optional(),
    time: z.unknown().optional(),
    error: z.unknown().optional(),
    metadata: z.record(z.string(), z.unknown()).nullish(),
  })),
});
const CompletedReplyTime = z.object({ completed: z.number().positive() });

/** Read the user turn paired with the final completed reply in a sealed host transcript. */
export function replayUserRequest(context: string, host: string): string | undefined {
  let decoded: unknown;
  try {
    decoded = JSON.parse(context);
  } catch {
    return undefined;
  }
  const parsed = ReplayContextSchema.safeParse(decoded);
  if (!parsed.success) return undefined;

  const { messages } = parsed.data;
  const anchor = messages.at(-1);
  if (anchor?.type !== 'assistant' || anchor.finish !== 'stop' ||
      anchor.error || !CompletedReplyTime.safeParse(anchor.time).success) return undefined;

  let user: (typeof messages)[number] | undefined;
  let businessUser: typeof user;
  const intent = `${host}.intent`;
  for (const message of messages) {
    if (message.type === 'user') {
      user = message;
      businessUser = message.metadata?.[intent] === 'recipe-authoring' ? undefined : message;
    } else if (message.type === 'idle') {
      user = undefined;
    } else if (message.type === 'synthetic') {
      const source = message.metadata?.source;
      user = source === 'subagent' || source === 'experimental_image_generate' ? businessUser : undefined;
    }
  }
  if (user?.metadata?.[`${host}.client`] !== 'desktop' ||
      user.metadata[intent] === 'recipe-authoring') return undefined;

  const input = TaskInputTextSchema.safeParse(user.text);
  return input.success ? input.data : undefined;
}
