/** Explicit Agent entry; no legacy Run HTTP. */
import { Runner } from '@openai/agents';
import { TaskWorkflowRunner } from './task-runner.js';
import { CoSensClient } from './atoms.js';
import type { CoSensOptions } from './openai-proxy.js';
import { TurnStore, hash } from './state.js';
import { RSIRecoveryRequired } from './errors.js';

export interface AgentRunnerOptions extends CoSensOptions { runner?: { run(agent:any,input:any,options?:any):Promise<any> }; fetcher?:typeof fetch }
export class CoSensAgentRunner {
 readonly native:any;
 readonly cosens:any;
 private readonly workflow:TaskWorkflowRunner;
 private readonly client:CoSensClient;
 private readonly store:TurnStore;
 constructor(agent:any,private readonly options:AgentRunnerOptions={}){
  this.native=agent;
  this.client=new CoSensClient({...options,fetch:options.fetch??options.fetcher});
  this.store=new TurnStore(options.statePath,this.client.transport.apiKey,this.client.transport.service.identity);
  this.workflow=new TaskWorkflowRunner((options.runner??new Runner()) as Runner,{...options,mode:options.mode==='approval'?'approval':'auto'});
  this.cosens=this.workflow.cosens;
 }
 async run(agent:any,input:any,options:Record<string,any>={}){
  const {inputArtifacts=[],turnId='agent:'+hash([input,options.session?.sessionId??options.session?.session_id??options.session?.id??null,options.session?.sessionIncarnation??'0']),authorize,...nativeOptions}=options;
  if(!nativeOptions.session&&this.options.session)nativeOptions.session=this.options.session;
  const key='agent-input:'+turnId;
  let saved=this.store.get<any>(key);
  const signature=hash([input,inputArtifacts,options.session?.sessionIncarnation??'0']);
  if(saved?.signature!==undefined&&saved.signature!==signature)throw new RSIRecoveryRequired('Input changed for the same turn');
  if(!saved){saved={signature,refs:[],pending:false,materials:[{name:'input.json',content:JSON.stringify({input})},...inputArtifacts]};this.store.put(key,saved);}
  if(saved.pending)throw new RSIRecoveryRequired('Input artifact receipt unknown');
  for(const material of saved.materials.slice(saved.refs.length)){
   saved.pending=true;this.store.put(key,saved);const ref=await this.client.atoms.artifact(material);if(typeof ref.artifact_id!=='string'||!ref.artifact_id||typeof ref.content_hash!=='string'||!ref.content_hash)throw new RSIRecoveryRequired('Artifact receipt missing ID or hash');saved.refs.push(ref.artifact_id);saved.pending=false;this.store.put(key,saved);
  }
  return this.workflow.run(agent,input,{...nativeOptions,rsi:{turnId,task:{source:'new_task',objective:String(input),artifacts:saved.refs},authorize}});
 }
 async reconcile(id:string,options:{session?:any}={}):Promise<any>{return this.workflow.reconcile(id,options);}
 async pendingState(agent:any,id:string,options:{session?:any}={}){return this.workflow.pendingState(agent,id,options);}
 async resume(agent:any,id:string,state:any,options:Record<string,any>={}){return this.workflow.resume(agent,id,state,options);}
 async close(){await this.workflow.close();await this.client.close();this.store.close();}
}
