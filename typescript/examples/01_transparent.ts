import OpenAI from 'openai';
import { cosens } from '../src/index.js';
export async function runTask(task:string,model:string){
 const modelClient=new OpenAI({baseURL:process.env.COSENS_MODEL_BASE_URL,apiKey:process.env.COSENS_API_KEY});
 const client=cosens(modelClient,{mode:'auto'});
 const response=await client.responses.create({model,input:task,store:false});
 return {client,response,turnId:client.cosens.lastTurnId};
}
// Invoke with the user's own words about the artifact; learning needs that feedback.
export async function onArtifactConfirmed(client:ReturnType<typeof cosens>,turnId:string,userWords:string){
 await client.cosens.feedback(userWords,{source:'human',turnId});
 return client.cosens.generateRecipe({turnId});
}
