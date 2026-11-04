/** Internal structured decisions follow the Friday Chat contract. */
const GATEWAYS=new Set(['https://llm.cosens.ai/v1','https://friday-llm-gateway.corpintra.rosettalab.top/v1']);
export async function structuredResponse(native:any,options:any,requestOptions:any={}){
 if(!GATEWAYS.has(String(native.baseURL??'').replace(/\/+$/,'')))return native.responses.create(options,requestOptions);
 const schema=options.text.format;
 const response=await native.chat.completions.create({model:options.model,messages:[{role:'user',content:options.input}],response_format:{type:'json_schema',json_schema:{name:schema.name,strict:schema.strict,schema:schema.schema}},...(options.max_output_tokens===undefined?{}:{max_completion_tokens:options.max_output_tokens})},requestOptions);
 return {output_text:response.choices[0].message.content};
}
