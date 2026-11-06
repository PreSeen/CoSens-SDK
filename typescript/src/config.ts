export type Region = 'us';
export interface Service { region: Region; url: string; identity: string }
const SERVICES: Record<Region, Service> = {
  us: { region: 'us', url: 'https://rsi.cosens.ai/api', identity: 'rsi.cosens.ai/api' },
};

export function resolveService(region?: string): Service {
  const chosen = (region ?? process.env.COSENS_REGION ?? 'us').trim().toLowerCase();
  if (chosen !== 'us') throw new TypeError('COSENS_REGION must be us');
  return SERVICES[chosen];
}

export function resolveApiKey(key?: string): string {
  const value = key ?? process.env.COSENS_API_KEY;
  if (!value || /\s/.test(value)) throw new TypeError('CoSens apiKey must be the bare CoSens API Key');
  return value;
}
