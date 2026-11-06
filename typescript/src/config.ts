export type Region = 'cn' | 'us';
export interface Service { region: Region; url: string; identity: string }
const SERVICES: Record<Region, Service> = {
  cn: { region: 'cn', url: 'https://friday-rsi-cn.stardust-ai.com/api', identity: 'friday-rsi-cn.stardust-ai.com/api' },
  us: { region: 'us', url: 'https://rsi.cosens.ai/api', identity: 'rsi.cosens.ai/api' },
};

export function resolveService(region?: string): Service {
  const chosen = (region ?? process.env.COSENS_REGION ?? 'cn').trim().toLowerCase();
  if (chosen !== 'cn' && chosen !== 'us') throw new TypeError('COSENS_REGION must be cn or us');
  return SERVICES[chosen];
}

export function resolveApiKey(key?: string): string {
  const value = key ?? process.env.COSENS_API_KEY;
  if (!value || /\s/.test(value)) throw new TypeError('CoSens apiKey must be the bare CoSens API Key');
  return value;
}
