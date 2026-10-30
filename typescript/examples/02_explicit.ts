import { Agent, OpenAIResponsesModel, Runner } from '@openai/agents';
import OpenAI from 'openai';
import { cosens } from '../src/index.ts';
import { fridayKey, launch, modelSettings } from './common.ts';

await launch(async () => {
  const apiKey = fridayKey();
  const { apiKey: modelApiKey, model, baseURL } = modelSettings();
  const native = new OpenAI({ apiKey: modelApiKey, baseURL });
  const agent = new Agent({ name: 'assistant', instructions: '按用户要求完成任务，明确区分事实与推测。', model: new OpenAIResponsesModel(native, model) });
  const runner = cosens(agent, { apiKey, mode: 'disable', statePath: process.env.COSENS_STATE_PATH, runner: new Runner({ tracingDisabled: true }) });
  try {
    const result = await runner.run(agent, process.env.COSENS_INPUT || '请简要说明如何编写可重复运行的集成测试。');
    console.log(result.finalOutput);
    const turn = runner.cosens.getTurn(runner.cosens.lastTurnId);
    console.log(JSON.stringify({ turnId: runner.cosens.lastTurnId, runId: turn?.runId ?? null, state: turn?.state ?? null }));
  } finally { await runner.close(); }
});
