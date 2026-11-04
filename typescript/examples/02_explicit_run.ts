import { Agent } from '@openai/agents';
import { CoSensAgentRunner } from '../src/index.js';
export async function runTask(task:string){
 const agent=new Agent({name:'assistant',instructions:'Complete the requested task.'});
 const client=new CoSensAgentRunner(agent,{mode:'auto'});
 const result=await client.run(agent,task);
 return {client,result,turnId:client.cosens.lastTurnId};
}
export function onArtifactConfirmed(client:CoSensAgentRunner,turnId:string,requestKey:string){return client.cosens.generateRecipe(turnId,{requestKey});}
