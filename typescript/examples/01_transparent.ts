import OpenAI from 'openai';
import { cosens } from '../src/index.js';
export async function runTask(task:string){
 const client=cosens(new OpenAI(),{mode:'auto'});
 const response=await client.responses.create({model:'gpt-4.1-mini',input:task});
 return {client,response,turnId:client.cosens.lastTurnId};
}
// Invoke only from the application's external artifact-confirmation handler.
export function onArtifactConfirmed(client:ReturnType<typeof cosens>,turnId:string,requestKey:string){return client.cosens.generateRecipe({turnId,requestKey});}
