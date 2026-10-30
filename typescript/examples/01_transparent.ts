import OpenAI from 'openai';
import { cosens } from '../src/index.ts';
import { fridayKey, launch, modelSettings } from './common.ts';

await launch(async () => {
  const apiKey = fridayKey();
  const { model, ...modelOptions } = modelSettings();
  const native = new OpenAI(modelOptions);
  const mode = process.env.COSENS_MODE === 'disable' ? 'disable' : 'auto';
  const client = cosens(native, { apiKey, mode, statePath: process.env.COSENS_STATE_PATH });
  try {
    const response = await client.responses.create({ model, input: process.env.COSENS_INPUT || '请简要说明如何编写可重复运行的集成测试。' });
    console.log(response.output_text);
    const turn = client.cosens.getTurn(client.cosens.lastTurnId);
    console.log(JSON.stringify({ turnId: client.cosens.lastTurnId, runId: turn?.runId ?? null, state: turn?.state ?? null }));
  } finally { await client.close(); }
});
