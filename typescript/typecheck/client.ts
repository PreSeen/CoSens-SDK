import { CoSensClient, cosens } from '../src/index.js';
const client=new CoSensClient({apiKey:'service-key'});
const model='public-model';
client.atoms.createTask({source:'new_task',objective:'Produce a report',selfContained:true,key:'task'});
client.atoms.startExecution('task',{key:'execution',model});
client.atoms.recordFeedback('execution',{feedback:'Check format',source:'human',key:'feedback'});
client.atoms.startExecution('task',{key:'correction',previousExecutionId:'execution'});
client.atoms.learn('execution',{model,maxCompletionTokens:200_000});
client.atoms.resumeLearning('execution');
client.atoms.waitLearning('execution',{timeout:60});
client.atoms.listRecipes({limit:100,origin:'generated'});
client.atoms.uploadRecipe('team_recipe',{version:2,previousVersion:1,recipe:{title:'T'}});
client.start('Produce a report',{requestKey:'task'});
client.start({task_id:'task',execution_id:'execution'},{phase:'correct',requestKey:'correction'});
client.start({execution_id:'execution'},{phase:'learn'});
client.atoms.startExecution('task',{key:'execution-memory',recallMemory:true});
// @ts-expect-error The old option name is gone.
client.atoms.startExecution('task',{key:'execution-old',includeMemory:true});
// @ts-expect-error Feedback must say where it comes from.
client.atoms.recordFeedback('execution',{feedback:'Check format',key:'feedback-no-source'});
// @ts-expect-error Learning is idempotent per execution and takes no key.
client.atoms.learn('execution',{key:'confirmed'});
// @ts-expect-error Revisions are folded into executions.
client.atoms.startRevision('revision',{key:'revision'});
// @ts-expect-error Recipe candidates are an execution's learning now.
client.atoms.startRecipeCandidate('task',{revisionId:'revision',key:'confirmed'});
// @ts-expect-error The phases are prepare, correct and learn.
client.start({task_id:'task'},{phase:'recipe',requestKey:'confirmed'});
// @ts-expect-error Run protocol has been removed.
client.atoms.prepare({objective:'task',key:'old'});
// @ts-expect-error Attribution is internal to Recipe generation.
client.atoms.startAttribution('revision',{key:'old'});
// @ts-expect-error Credentials are taken from Authorization, not model configuration.
client.atoms.learn('execution',{modelApi:{model:'model',apiKey:'key'}});
void cosens;
