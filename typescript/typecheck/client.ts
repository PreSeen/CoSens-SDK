import { Runner } from '@openai/agents';
import { CoSensClient, TaskWorkflowRunner, cosens } from '../src/index.js';
const client=new CoSensClient({apiKey:'service-key'});
const model='public-model';
client.atoms.createTask({objective:'Produce a report',key:'task'});
client.atoms.createTask({objective:'Produce a report',artifacts:['brief',{artifact_id:'template',role:'reference'}],key:'task-refs'});
client.atoms.supplementTaskContext('task',{expectedContextVersion:1,objective:'Produce a report',artifacts:['brief']});
client.atoms.startExecution('task',{key:'execution',model,source:'new_task',objective:'x'});
client.atoms.recordFeedback('execution',{feedback:'Check format',source:'human',key:'feedback'});
client.atoms.startExecution(undefined,{key:'correction',previousExecutionId:'execution'});
client.atoms.startExecution('task',{key:'correction-task',previousExecutionId:'execution'});
client.atoms.learn('execution',{model,maxCompletionTokens:200_000});
client.atoms.resumeLearning('execution');
client.atoms.waitLearning('execution',{timeout:60});
client.atoms.listRecipes({limit:100,origin:'generated'});
client.atoms.uploadRecipe('team_recipe',{version:2,previousVersion:1,recipe:{title:'T'}});
client.start('Produce a report',{requestKey:'task'});
client.start({task_id:'task',execution_id:'execution'},{phase:'correct',requestKey:'correction'});
client.start({execution_id:'execution'},{phase:'learn'});
client.atoms.startExecution('task',{key:'execution-memory',source:'new_task',objective:'x',recallMemory:true});
client.atoms.startExecution('task',{key:'execution-external',source:'external_artifact',artifacts:['output',{artifact_id:'input',role:'original_input'}],selfContained:true,recipeId:'recipe'});
// @ts-expect-error The old option name is gone.
client.atoms.startExecution('task',{key:'execution-old',source:'new_task',objective:'x',includeMemory:true});
// @ts-expect-error A new execution names its source.
client.atoms.startExecution('task',{key:'execution-no-source',objective:'x'});
// @ts-expect-error A task is a capability: its artifacts are references.
client.atoms.createTask({objective:'Produce a report',artifacts:[{artifact_id:'input',role:'original_input'}],key:'task-input'});
// @ts-expect-error A task takes no source; the execution does.
client.atoms.createTask({source:'new_task',objective:'Produce a report',key:'task-source'});
// @ts-expect-error The execution input says whether it is self-contained, not the task.
client.atoms.supplementTaskContext('task',{expectedContextVersion:1,selfContained:true});
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
// @ts-expect-error A correction takes no Recipe.
client.atoms.startExecution('task',{key:'correction-recipe',previousExecutionId:'execution',recipeId:'recipe'});
// @ts-expect-error A correction does not recall Memory.
client.atoms.startExecution('task',{key:'correction-memory',previousExecutionId:'execution',recallMemory:true});
// @ts-expect-error A correction keeps the input of the execution it corrects.
client.atoms.startExecution('task',{key:'correction-source',previousExecutionId:'execution',source:'new_task'});
// @ts-expect-error A correction keeps the objective of the execution it corrects.
client.atoms.startExecution('task',{key:'correction-objective',previousExecutionId:'execution',objective:'x'});
client.atoms.startExecution('task',{key:'correction-model',previousExecutionId:'execution',model,recallMemory:false});
// @ts-expect-error A snapshot names the execution, not a revision.
client.start({task_id:'task',revision_id:'revision'},{phase:'correct',requestKey:'correction'});

const wrapped=cosens({responses:{create:async(_params:unknown)=>({})}});
wrapped.cosens.generateRecipe({turnId:'turn',maxCompletionTokens:200_000});
// @ts-expect-error Learning is idempotent per execution and takes no key.
wrapped.cosens.generateRecipe({turnId:'turn',requestKey:'confirmed'});
const runner=new TaskWorkflowRunner(Runner,{mode:'auto'});
runner.cosens.generateRecipe('turn',{model,maxCompletionTokens:200_000});
// @ts-expect-error Learning is idempotent per execution and takes no key.
runner.cosens.generateRecipe('turn',{requestKey:'confirmed'});
