import OpenAI from 'openai';
import { Agent, setDefaultOpenAIClient, setTracingDisabled } from '@openai/agents';
import { CoSensAgentRunner } from '../src/index.js';
export async function runTask(task:string,model:string){
 setDefaultOpenAIClient(new OpenAI({baseURL:process.env.COSENS_MODEL_BASE_URL,apiKey:process.env.COSENS_API_KEY}));
 setTracingDisabled(true);
 const agent=new Agent({name:'assistant',instructions:'Complete the requested task.',model});
 const client=new CoSensAgentRunner(agent,{mode:'auto'});
 const result=await client.run(agent,task);
 return {client,result,turnId:client.cosens.lastTurnId};
}
export function onArtifactConfirmed(client:CoSensAgentRunner,turnId:string,requestKey:string){return client.cosens.generateRecipe(turnId,{requestKey});}
