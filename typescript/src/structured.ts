import { RSIDecisionUnavailable } from './errors.js';

/**
 * Internal structured decisions use Chat Completions with a JSON schema response format through the developer's native client.
 * `extra_body` (for example `{ enable_thinking: false }`) is merged into the request body; callers pass
 * `requestOptions` such as `{ timeout, maxRetries: 0 }` for the OpenAI client.
 */
export async function structuredResponse(native:any,options:any,requestOptions:any={}){
 const schema=options.text.format;
 const response=await native.chat.completions.create({model:options.model,messages:[{role:'user',content:options.input}],response_format:{type:'json_schema',json_schema:{name:schema.name,strict:schema.strict,schema:schema.schema}},...(options.max_output_tokens===undefined?{}:{max_completion_tokens:options.max_output_tokens}),...(options.extra_body??{})},requestOptions);
 const choice=response.choices[0];
 // Reasoning models can spend the whole budget before writing the JSON.
 if(choice.finish_reason==='length')throw new RSIDecisionUnavailable(`Policy output was cut off at max_output_tokens=${options.max_output_tokens} (finish_reason=length); disable reasoning for the decision or use a non-reasoning decisionClient`);
 return {output_text:choice.message.content};
}
