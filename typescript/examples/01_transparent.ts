import { cosens, fridayGatewayClient } from '../src/index.js';
export async function runTask(task:string,model:string){
 const client=cosens(fridayGatewayClient(),{mode:'auto'});
 const response=await client.responses.create({model,input:task,store:false});
 return {client,response,turnId:client.cosens.lastTurnId};
}
// Invoke from the application's external artifact-confirmation handler.
export function onArtifactConfirmed(client:ReturnType<typeof cosens>,turnId:string,requestKey:string){return client.cosens.generateRecipe({turnId,requestKey});}
