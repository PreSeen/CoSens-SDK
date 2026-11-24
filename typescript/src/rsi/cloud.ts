import { openAsBlob } from 'node:fs';
import { readFile } from 'node:fs/promises';
import { basename } from 'node:path';
import { fileURLToPath } from 'node:url';
import { selectedVirtualUser, type VirtualUserSelection } from '../virtual-user.js';
import { USER_AGENT } from './chat-completions.js';
import { readRecipeFile } from './recipe-file.js';
import {
  RecipeReferenceSchema,
  RSIError,
  type Recipe,
  type RecipeReference,
} from './models.js';

export const DEFAULT_CLOUD_BASE_URL = 'https://rsi.cosens.ai/api';
/** The CoSens gateway accepts request bodies up to 10 MiB. */
export const MAX_CLOUD_REQUEST_BYTES = 10 * 1024 * 1024;
const MAX_RECIPE_ZIP_BYTES = 8 * 1024 * 1024;

export type CloudOptions = {
  api_key?: string;
  base_url?: string;
  /** Request timeout in seconds (default 60). */
  timeout?: number;
} & VirtualUserSelection;
export type RecipeUpsertOptions = { recipe_id?: string; idempotency_key?: string };
export type RecipeUpsertResult = { recipe_id: string; recipe_version_id: string };

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
  readonly #timeoutMs: number;

  constructor(options: CloudOptions = {}) {
    const key = options.api_key ?? process.env.COSENS_API_KEY;
    if (!key)
      throw new TypeError('cloud mode needs COSENS_API_KEY (or api_key)');
    this.#key = key;
    this.#virtualUserId = selectedVirtualUser(options);
    const timeout = options.timeout ?? 60;
    if (typeof timeout !== 'number' || !Number.isFinite(timeout) || timeout <= 0)
      throw new TypeError('timeout must be a positive number of seconds');
    this.#timeoutMs = timeout * 1000;
    this.#base = (
      options.base_url ??
      process.env.COSENS_RSI_BASE_URL ??
      DEFAULT_CLOUD_BASE_URL
    ).replace(/\/+$/, '');
  }

  async request(
    method: string,
    path: string,
    {
      body, params, multipart, idempotency_key, binary,
    }: {
      body?: unknown;
      params?: Record<string, string>;
      multipart?: FormData;
      idempotency_key?: string;
      binary?: boolean;
    } = {},
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
          ...(payload === undefined ? {} : { 'content-type': 'application/json' }),
          ...(idempotency_key === undefined ? {} : { 'Idempotency-Key': idempotency_key }),
        },
        body: multipart ?? payload,
        redirect: 'error',
        signal: AbortSignal.timeout(this.#timeoutMs),
      });
    } catch {
      throw new RSIError(
        'service_unavailable',
        'CoSens RSI service could not be reached',
      );
    }
    if (response.ok && binary) return new Uint8Array(await response.arrayBuffer());
    const answer = await response.json().catch(() => undefined);
    if (response.ok) return answer;
    const detail = (answer as { error?: { code?: string; message?: string } })
      ?.error;
    const code = detail?.code ?? `http_${response.status}`;
    const message =
      detail?.message ?? `CoSens RSI returned HTTP ${response.status}`;
    if (response.status === 422 && code === 'invalid_request')
      throw new TypeError(message);
    throw new RSIError(code, message, response.status);
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

  async recipe_insert(path: string): Promise<Recipe> {
    // Validate locally first, so a malformed file fails the same way in both modes.
    const { markdown } = await readRecipeFile(path);
    return this.request('POST', '/recipes', { body: { markdown } });
  }

  async recipes_upsert(
    path: string | URL,
    { recipe_id, idempotency_key }: RecipeUpsertOptions = {},
  ): Promise<RecipeUpsertResult> {
    let localPath: string;
    if (path instanceof URL || /^file:/i.test(path)) {
      const uri = new URL(path);
      if (uri.protocol !== 'file:' || (uri.hostname && uri.hostname !== 'localhost') || uri.search || uri.hash)
        throw new TypeError('Recipe file URI must refer to a local path');
      localPath = fileURLToPath(uri);
    } else {
      localPath = path;
    }
    if (
      recipe_id !== undefined
      && (typeof recipe_id !== 'string' || !/^[A-Za-z0-9_-]{1,64}$/.test(recipe_id))
    ) {
      throw new TypeError('recipe_id must contain 1–64 letters, digits, underscores or hyphens');
    }
    if (
      idempotency_key !== undefined
      && (typeof idempotency_key !== 'string'
        || !/^[\x20-\x7e]+$/.test(idempotency_key)
        || idempotency_key.trim() !== idempotency_key)
    ) {
      throw new TypeError('idempotency_key must be a nonempty HTTP header value');
    }
    const zip = await openAsBlob(localPath, { type: 'application/zip' });
    if (zip.size === 0 || zip.size > MAX_RECIPE_ZIP_BYTES)
      throw new TypeError('Recipe ZIP must be nonempty and at most 8 MiB');
    const multipart = new FormData();
    multipart.set('file', zip, basename(localPath));
    if (recipe_id !== undefined) multipart.set('recipe_id', recipe_id);
    return this.request('POST', '/recipes_upsert', { multipart, idempotency_key });
  }

  recipe_get(id: string, version = 'active'): Promise<Recipe> {
    return this.request('GET', `/recipes/${segment(id)}`, {
      params: { version },
    });
  }

  recipe_download(id: string, recipeVersionId: string): Promise<Uint8Array> {
    return this.request(
      'GET',
      `/recipes/${segment(id)}/versions/${segment(recipeVersionId)}/download`,
      { binary: true },
    );
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
