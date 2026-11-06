/** Internal structured decisions use Chat Completions with a JSON schema response format through the developer's native client. */
export async function structuredResponse(native:any,options:any,requestOptions:any={}){
 const schema=options.text.format;
 const response=await native.chat.completions.create({model:options.model,messages:[{role:'user',content:options.input}],response_format:{type:'json_schema',json_schema:{name:schema.name,strict:schema.strict,schema:schema.schema}},...(options.max_output_tokens===undefined?{}:{max_completion_tokens:options.max_output_tokens})},requestOptions);
 return {output_text:response.choices[0].message.content};
}
