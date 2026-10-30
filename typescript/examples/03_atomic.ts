import { randomUUID } from 'node:crypto';
import OpenAI from 'openai';
import { CoSensClient } from '../src/index.ts';
import { fridayKey, launch, modelSettings } from './common.ts';

await launch(async () => {
  const apiKey = fridayKey();
  const { model, ...modelOptions } = modelSettings();
  const prompt = process.env.COSENS_INPUT || '请简要说明如何编写可重复运行的集成测试。';
  const turnId = process.env.COSENS_TURN_ID || randomUUID();
  console.log(JSON.stringify({ turnId, note: 'Persist this ID; only pre-execution retries may reuse it automatically' }));
  const rsi = new CoSensClient({ apiKey });
  const native = new OpenAI(modelOptions);
  try {
    const prepared = await rsi.atoms.prepareReady({ objective: prompt, key: turnId });
    const run = await rsi.atoms.getRun(prepared.run_id);
    if (run.run_id !== prepared.run_id || run.status !== 'ready' || !run.snapshot) throw new Error('Run context is not ready');
    const frozen = { recipe: prepared.recipe, context: prepared.context, snapshot: run.snapshot };
    const response = await native.responses.create({ model, input: prompt, instructions: `[CoSens frozen context; source: RSI Run ${run.run_id}]\n${JSON.stringify(frozen)}` });
    if (response.status !== 'completed' || response.output.some(item => item.type === 'function_call')) throw new Error('Model response needs continuation; do not submit this Run');
    const summary = response.output_text;
    const events = [
      { id: `${turnId}:user`, kind: 'user_prompt', payload: { role: 'user', content: prompt } },
      { id: `${turnId}:assistant`, kind: 'agent_action', payload: { role: 'assistant', content: summary } },
    ];
    const ack = await rsi.atoms.sessionEvents(run.run_id, events);
    if (!Array.isArray(ack.durable_ack) || events.some(event => !ack.durable_ack.includes(event.id))) throw new Error('Session events were not fully acknowledged; reconcile before retrying');
    const receipt = await rsi.atoms.submit(run.run_id, { data: { summary, outcome: 'completed' } });
    console.log(JSON.stringify({ runId: run.run_id, status: receipt.status, summary }));
  } finally { await rsi.close(); }
});
