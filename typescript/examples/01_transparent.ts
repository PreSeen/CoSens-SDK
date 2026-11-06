import OpenAI from 'openai';
import { cosens } from '../src/index.js';
export async function runTask(task:string,model:string){
 const modelClient=new OpenAI({baseURL:process.env.COSENS_MODEL_BASE_URL,apiKey:process.env.COSENS_API_KEY});
 const client=cosens(modelClient,{mode:'auto'});
 const response=await client.responses.create({model,input:task,store:false});
 return {client,response,turnId:client.cosens.lastTurnId};
}
// Invoke from the application's external artifact-confirmation handler.
export function onArtifactConfirmed(client:ReturnType<typeof cosens>,turnId:string,requestKey:string){return client.cosens.generateRecipe({turnId,requestKey});}
