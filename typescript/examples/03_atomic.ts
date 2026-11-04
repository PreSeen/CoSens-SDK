/** Host execution and external confirmation stay explicit. */
import { CoSensClient } from '../src/index.js';

export async function recordTask(client:CoSensClient,objective:string,artifactContent:string,model:string){
 const task=await client.atoms.createTask({source:'new_task',objective,selfContained:true,key:crypto.randomUUID()});
 const execution=await client.atoms.startExecution(task.task_id,{key:crypto.randomUUID(),model});
 const artifact=await client.atoms.artifact({name:'output.txt',content:artifactContent});
 const result=await client.atoms.submitExecution(execution.execution_id,{requestSha256:execution.request_sha256,outcome:'completed',artifacts:[{artifact_id:artifact.artifact_id,role:'submitted_output'}]});
 return {taskId:task.task_id,revisionId:result.revision_id};
}

// Call this only from the application's artifact-confirmation handler.
export function generateConfirmedRecipe(client:CoSensClient,taskId:string,revisionId:string,requestKey:string){
 return client.atoms.startRecipeCandidate(taskId,{revisionId,key:requestKey});
}
