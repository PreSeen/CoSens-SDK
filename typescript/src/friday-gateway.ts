import OpenAI from 'openai';
import { resolveApiKey, resolveService, type Region } from './config.js';

export const FRIDAY_US_BASE_URL = 'https://llm.cosens.ai/v1';
const GATEWAYS:Record<Region,string>={cn:'https://friday-llm-gateway.corpintra.rosettalab.top/v1',us:FRIDAY_US_BASE_URL};

/** CoSens and the regional model gateway share COSENS_API_KEY. */
export function fridayGatewayClient(apiKey?:string,options:{region?:Region}={}):OpenAI{
 if(!options||typeof options!=='object'||Array.isArray(options)||Object.keys(options).some(k=>k!=='region'))throw new TypeError('Only region is configurable; API Key authentication and gateway URL are automatic');
 const service=resolveService(options.region);
 const key=resolveApiKey(apiKey);
 return new OpenAI({apiKey:key,baseURL:GATEWAYS[service.region],defaultHeaders:{'X-Friday-Auth-Type':'api_key'},maxRetries:0});
}
