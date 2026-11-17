/** Host execution and external confirmation stay explicit. */
import { CoSensClient } from '../src/index.js';

export async function recordTask(client:CoSensClient,objective:string,artifactContent:string,model:string){
 const task=await client.atoms.createTask({objective,key:crypto.randomUUID()});
 const execution=await client.atoms.startExecution(task.task_id,{key:crypto.randomUUID(),model,source:'new_task',objective,selfContained:true});
 const artifact=await client.atoms.artifact({name:'output.txt',content:artifactContent});
 const result=await client.atoms.submitExecution(execution.execution_id,{requestSha256:execution.request_sha256,outcome:'completed',artifacts:[{artifact_id:artifact.artifact_id,role:'submitted_output'}]});
 if(result.status!=='completed')throw new Error('The execution result was not confirmed');
 return {taskId:task.task_id,executionId:result.execution_id};
}

// Call this only from the application's artifact-confirmation handler.
export async function learnConfirmedRecipe(client:CoSensClient,executionId:string){
 await client.atoms.learn(executionId);
 return client.atoms.waitLearning(executionId);
}
