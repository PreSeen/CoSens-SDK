import OpenAI from 'openai';
import { Agent } from '@openai/agents';
import { cosens, type CoSensProxyExtras, CoSensAgentRunner } from '../src/index.js';

const openai = new OpenAI({ apiKey: 'model-key' });
const client = cosens(openai, { apiKey: 'friday-key', mode: 'disable' });
const response = client.responses.create({ model: 'model', input: 'hello' });
response.asResponse();
client.models.list();
client.native.responses.create({ model: 'model', input: 'hello' });
const extras: CoSensProxyExtras<OpenAI> = client;
extras.cosens.getTurn('turn-id');
const derived = client.withOptions({ timeout: 1_000 });
derived.cosens.getTurn(derived.cosens.lastTurnId);
// @ts-expect-error Native OpenAI resources do not contain this method.
client.responses.nonexistentMethod();

const agent = new Agent({ name: 'helper', instructions: 'Do work' });
const runner: CoSensAgentRunner = cosens(agent, { apiKey: 'friday-key', mode: 'disable' });
void runner;
