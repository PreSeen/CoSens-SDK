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
// Invoke with the user's own words about the artifact; learning needs that feedback.
export async function onArtifactConfirmed(client:CoSensAgentRunner,turnId:string,userWords:string){
 await client.cosens.feedback(userWords,'human',turnId);
 return client.cosens.generateRecipe(turnId);
}
