import { readFile } from 'node:fs/promises';
import { basename } from 'node:path';
import { selectedVirtualUser, type VirtualUserSelection } from '../virtual-user.js';
import { USER_AGENT } from './chat-completions.js';
import { readRecipeFile } from './recipe-file.js';
import {
  RecipeReferenceSchema,
  RSIError,
  type RecipeReference,
} from './models.js';

export const DEFAULT_CLOUD_BASE_URL = 'https://rsi.cosens.ai/api';
/** The CoSens gateway accepts request bodies up to 10 MiB. */
export const MAX_CLOUD_REQUEST_BYTES = 10 * 1024 * 1024;

export type CloudOptions = { api_key?: string; base_url?: string } & VirtualUserSelection;

type EvidenceInput = {
  context_path: string;
  files: { path: string; role: string }[];
};

type Upload = {
  name: string;
  content: string;
  encoding: 'utf-8' | 'base64';
  role?: string;
  recipe_id?: string;
};

async function upload(
  path: string,
  extra: { role?: string; recipe_id?: string } = {},
): Promise<Upload> {
  const data = await readFile(path);
  let content: string;
  let encoding: 'utf-8' | 'base64';
  try {
    content = new TextDecoder('utf-8', { fatal: true }).decode(data);
    encoding = 'utf-8';
  } catch {
    content = data.toString('base64');
    encoding = 'base64';
  }
  return { name: basename(path), content, encoding, ...extra };
}

async function evidenceBody(
  evidence: EvidenceInput,
  recipeReference?: RecipeReference,
) {
  if (!evidence?.context_path || !Array.isArray(evidence.files))
    throw new TypeError('evidence needs context_path and files');
  const files = [];
  for (const file of evidence.files) {
    if (!file?.path || !file.role)
      throw new TypeError('each evidence file needs path and role');
    if (file.role === 'recipe')
      throw new TypeError(
        'supply the Recipe using execution_create.recipe_reference',
      );
    files.push(await upload(file.path, { role: file.role }));
  }
  if (recipeReference !== undefined) {
    const { path, recipe_id } = RecipeReferenceSchema.parse(recipeReference);
    files.push(await upload(path, { role: 'recipe', recipe_id }));
  }
  return { context: await upload(evidence.context_path), files };
}

function segment(value: string) {
  if (typeof value !== 'string' || !value || value === '.' || value === '..')
    throw new TypeError('ID must be a nonempty string');
  return encodeURIComponent(value);
}

/** Cloud mode uploads the supplied evidence and optional Recipe reference to CoSens. */
export class CloudClient {
  readonly #base: string;
  readonly #key: string;
  readonly #virtualUserId?: string;

  constructor(options: CloudOptions = {}) {
    const key = options.api_key ?? process.env.COSENS_API_KEY;
    if (!key)
      throw new TypeError('cloud mode needs COSENS_API_KEY (or api_key)');
    this.#key = key;
    this.#virtualUserId = selectedVirtualUser(options);
    this.#base = (
      options.base_url ??
      process.env.COSENS_RSI_BASE_URL ??
      DEFAULT_CLOUD_BASE_URL
    ).replace(/\/+$/, '');
  }

  async request(
    method: string,
    path: string,
    { body, params }: { body?: unknown; params?: Record<string, string> } = {},
  ): Promise<any> {
    const payload = body === undefined ? undefined : JSON.stringify(body);
    if (payload !== undefined && Buffer.byteLength(payload) > MAX_CLOUD_REQUEST_BYTES)
      throw new TypeError(
        'upload exceeds the 10 MiB cloud request limit; send fewer or smaller files, or use local mode',
      );
    const url = new URL(this.#base + path);
    for (const [key, value] of Object.entries(params ?? {}))
      url.searchParams.set(key, value);
    let response: Response;
    try {
      response = await fetch(url, {
        method,
        headers: {
          authorization: `Bearer ${this.#key}`,
          'x-auth-type': 'api_key',
          ...(this.#virtualUserId === undefined ? {} : { 'X-Virtual-User-Id': this.#virtualUserId }),
          'user-agent': USER_AGENT,
          ...(body === undefined ? {} : { 'content-type': 'application/json' }),
        },
        body: payload,
        redirect: 'error',
        signal: AbortSignal.timeout(60_000),
      });
    } catch {
      throw new RSIError(
        'service_unavailable',
        'CoSens RSI service could not be reached',
      );
    }
    const answer = await response.json().catch(() => undefined);
    if (response.ok) return answer;
    const detail = (answer as { error?: { code?: string; message?: string } })
      ?.error;
    const code = detail?.code ?? `http_${response.status}`;
    const message =
      detail?.message ?? `CoSens RSI returned HTTP ${response.status}`;
    if (response.status === 422 && code === 'invalid_request')
      throw new TypeError(message);
    throw new RSIError(code, message);
  }

  async execution_create(request: {
    evidence: EvidenceInput;
    idempotency_key: string;
    recipe_reference?: RecipeReference;
  }) {
    if (!request?.idempotency_key)
      throw new TypeError('idempotency_key must be a nonempty string');
    return this.request('POST', '/executions', {
      body: {
        idempotency_key: request.idempotency_key,
        ...(await evidenceBody(request.evidence, request.recipe_reference)),
      },
    });
  }

  execution_get(id: string) {
    return this.request('GET', `/executions/${segment(id)}`);
  }

  async execution_list(query?: { execution_ids: string[] }) {
    if (query && !query.execution_ids.length) return [];
    return this.request('GET', '/executions', {
      params: query ? { execution_ids: query.execution_ids.join(',') } : {},
    });
  }

  async execution_append_evidence(id: string, evidence: EvidenceInput) {
    const body = await evidenceBody(evidence);
    return this.request('POST', `/executions/${segment(id)}/evidence`, {
      body,
    });
  }

  execution_cancel(id: string) {
    return this.request('POST', `/executions/${segment(id)}/cancel`);
  }

  execution_clarification_answer(
    id: string,
    response: {
      question_id: string;
      question_revision: number;
      answer: string | string[];
    },
  ) {
    return this.request('POST', `/executions/${segment(id)}/clarifications`, {
      body: response,
    });
  }

  rubrics_list(query: { offset: number; archived: boolean }) {
    return this.request('GET', '/rubrics', {
      params: { offset: String(query.offset), archived: String(query.archived) },
    });
  }

  rubrics_get(id: string, version: string) {
    return this.request('GET', `/rubrics/${segment(id)}`, {
      params: { version },
    });
  }

  async recipe_list(query?: { recipe_ids: string[] }) {
    if (query && !query.recipe_ids.length) return [];
    return this.request('GET', '/recipes', {
      params: query ? { recipe_ids: query.recipe_ids.join(',') } : {},
    });
  }

  async recipe_insert(path: string) {
    // Validate locally first, so a malformed file fails the same way in both modes.
    const { markdown } = await readRecipeFile(path);
    return this.request('POST', '/recipes', { body: { markdown } });
  }

  recipe_get(id: string, version = 'active') {
    return this.request('GET', `/recipes/${segment(id)}`, {
      params: { version },
    });
  }

  recipe_test(request: {
    execution_id: string;
    recipe_id: string;
    version_id: string;
    rubrics_version_id: string;
  }) {
    const { execution_id, ...body } = request;
    return this.request(
      'POST',
      `/executions/${segment(execution_id)}/recipe-tests`,
      { body },
    );
  }

  recipe_version_set_archived(
    id: string,
    request: { version_id: string; archived: boolean },
  ) {
    return this.request(
      'PUT',
      `/recipes/${segment(id)}/versions/${segment(request.version_id)}/archived`,
      { body: { archived: request.archived } },
    );
  }
}
