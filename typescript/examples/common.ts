export class ConfigurationError extends Error {}

export function fridayKey(): string {
  const key = process.env.RSI_API_KEY || process.env.COSENS_API_KEY;
  if (!key || key.startsWith('your-') || /\s/.test(key)) throw new ConfigurationError('Set RSI_API_KEY to a Friday API Key before running this example');
  return key;
}

export function modelSettings() {
  const apiKey = process.env.OPENAI_API_KEY;
  const model = process.env.OPENAI_MODEL;
  if (!apiKey || apiKey.startsWith('your-')) throw new ConfigurationError('Set OPENAI_API_KEY before running this example');
  if (!model) throw new ConfigurationError('Set OPENAI_MODEL before running this example');
  return { apiKey, model, baseURL: process.env.OPENAI_BASE_URL || undefined };
}

export async function launch(work: () => Promise<void>) {
  try { await work(); }
  catch (error) {
    if (error instanceof ConfigurationError) console.error(error.message);
    else console.error(`Example failed: ${error instanceof Error ? error.constructor.name : 'Error'}; status=${(error as any)?.statusCode ?? 'unknown'}; requestId=${(error as any)?.requestId ?? 'none'}`);
    process.exitCode = 1;
  }
}
