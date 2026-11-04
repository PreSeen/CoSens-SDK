import { CoSensClient, cosens } from '../src/index.js';
const client=new CoSensClient({apiKey:'service-key'});
const model='public-model';
client.atoms.createTask({source:'new_task',objective:'Produce a report',selfContained:true,key:'task'});
client.atoms.startExecution('task',{key:'execution',model});
client.atoms.recordFeedback('revision',{feedback:'Check format',key:'feedback'});
client.atoms.startRevision('revision',{key:'revision'});
client.atoms.startRecipeCandidate('task',{revisionId:'revision',key:'confirmed',model});
client.atoms.listRecipeCandidates({status:'candidate'});
client.start('Produce a report',{requestKey:'task'});
client.start({task_id:'task',revision_id:'revision'},{phase:'recipe',requestKey:'confirmed'});
// @ts-expect-error Memory is always recalled for generation; there is no switch.
client.atoms.startRecipeCandidate('task',{revisionId:'revision',key:'confirmed',includeMemory:false});
// @ts-expect-error Run protocol has been removed.
client.atoms.prepare({objective:'task',key:'old'});
// @ts-expect-error Attribution is internal to Recipe generation.
client.atoms.startAttribution('revision',{key:'old'});

// @ts-expect-error Credentials are taken from Authorization, not model configuration.
client.atoms.startRecipeCandidate('task',{revisionId:'revision',key:'confirmed',modelApi:{model:'model',apiKey:'key'}});
