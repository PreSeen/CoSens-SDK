import OpenAI from 'openai';
import { Agent } from '@openai/agents';
import { cosens, type CoSensProxyExtras, CoSensAgentRunner, CoSensClient, type BusinessModelAPI } from '../src/index.js';

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

const tasks = new CoSensClient({ apiKey: 'gateway-key' });
const modelApi: BusinessModelAPI = { baseUrl: 'https://business.example/v1', model: 'business-model', apiKey: 'provider-key' };
tasks.atoms.startExecution('task-1', { key: 'execution-1', modelApi, recipeId: 'recipe-1', includeMemory: true });
tasks.atoms.improveTask('task-1', { key: 'review-1', revisionId: 'revision-1', feedback: 'Review units', modelApi });
tasks.atoms.getTaskRecipe('task-1');
tasks.atoms.getRecipeVersions('recipe-1');
tasks.atoms.selectCause('attribution-1', { customReason: 'Operator hypothesis' });
tasks.start('Compute a report', { requestKey: 'prepare-1', modelApi, recipeId: 'recipe-1', includeMemory: true });
tasks.start({ task_id: 'task-1', revision_id: 'revision-1', feedback: 'Review units' }, { phase: 'improve', requestKey: 'review-1', modelApi });
// @ts-expect-error SDK options use camelCase, not HTTP wire field names.
tasks.atoms.startExecution('task-1', { key: 'bad', model_api: modelApi });
// @ts-expect-error Memory opt-in requires a boolean.
tasks.start('Compute a report', { requestKey: 'bad', includeMemory: 'true' });
