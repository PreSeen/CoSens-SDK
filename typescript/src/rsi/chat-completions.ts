import { createRequire } from 'node:module';
import { z } from 'zod';
import { fail, RSIError, type LocalConfig } from './models.js';

function userAgent(): string {
  try {
    const pkg = createRequire(import.meta.url)('../../package.json') as {
      name?: string;
      version?: string;
    };
    if (pkg.name === 'cosens-sdk' && pkg.version)
      return `cosens-sdk-typescript/${pkg.version}`;
  } catch {
    // Bundled without package.json.
  }
  return 'cosens-sdk-typescript/source';
}

export const USER_AGENT = userAgent();

const toolCallSchema = z.object({
  id: z.string().min(1),
  type: z.literal('function'),
  function: z.object({ name: z.string().min(1), arguments: z.string() }),
});
const assistantSchema = z.object({
  role: z.literal('assistant'),
  content: z.string().nullish(),
  tool_calls: z.array(toolCallSchema).nullish(),
  reasoning_content: z.string().nullish(),
});
const responseSchema = z.object({
  choices: z.tuple([
    z.object({ message: assistantSchema, finish_reason: z.string().nullish() }),
  ]),
});

type Assistant = z.infer<typeof assistantSchema>;
export type Message =
  | Assistant
  | { role: 'system' | 'user'; content: string }
  | { role: 'tool'; tool_call_id: string; content: string };

export type FunctionDefinition = {
  name: string;
  description: string;
  parameters: object;
};

export interface Tool {
  definition: FunctionDefinition;
  invoke(args: unknown): unknown;
}

export function defineTool<S extends z.ZodType>(
  run: (args: z.output<S>) => unknown,
  {
    name,
    description,
    schema,
  }: { name: string; description: string; schema: S },
): Tool {
  return {
    definition: {
      name,
      description,
      parameters: z.toJSONSchema(schema, { io: 'input' }),
    },
    invoke(args) {
      const parsed = schema.safeParse(args);
      return parsed.success
        ? run(parsed.data)
        : 'Invalid arguments for this tool.';
    },
  };
}

class ModelStatusError extends Error {
  constructor(readonly status: number) {
    super('Model endpoint rejected the request');
  }
}

export async function requestCompletion(
  config: LocalConfig['model'],
  key: string,
  messages: Message[],
  tools: FunctionDefinition[],
): Promise<Assistant> {
  const base = config.base_url ?? 'https://api.openai.com/v1';
  const signal = AbortSignal.timeout(Math.ceil(config.timeout * 1000));
  let response: Response;
  try {
    response = await fetch(base.replace(/\/+$/, '') + '/chat/completions', {
      method: 'POST',
      headers: {
        authorization: 'Bearer ' + key,
        'content-type': 'application/json',
        'user-agent': USER_AGENT,
      },
      body: JSON.stringify({
        model: config.model,
        ...(config.max_tokens === undefined
          ? {}
          : { max_tokens: config.max_tokens }),
        messages,
        tools: tools.map((definition) => ({
          type: 'function',
          function: definition,
        })),
        tool_choice: 'auto',
      }),
      redirect: 'error',
      signal,
    });
  } catch {
    throw new RSIError(
      signal.aborted ? 'timeout' : 'model_unavailable',
      'Model request could not be completed',
    );
  }
  if (!response.ok) {
    await response.body?.cancel().catch(() => undefined);
    throw new ModelStatusError(response.status);
  }
  const raw = await response.json().catch(() => {
    if (signal.aborted) fail('timeout', 'Model response timed out');
    return fail('invalid_model_output', 'Model returned unreadable JSON');
  });
  const parsed = responseSchema.safeParse(raw);
  if (!parsed.success)
    return fail('invalid_model_output', 'Invalid model response envelope');
  const { message, finish_reason } = parsed.data.choices[0];
  if (finish_reason === 'length' || finish_reason === 'content_filter')
    return fail(
      'invalid_model_output',
      'Model response ended before completion',
    );
  const ids = message.tool_calls?.map((call) => call.id) ?? [];
  if (new Set(ids).size !== ids.length)
    return fail(
      'invalid_model_output',
      'Model repeated tool call IDs in one response',
    );
  return message;
}
