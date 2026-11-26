import { openAsBlob } from 'node:fs';
import { readFile } from 'node:fs/promises';
import { basename } from 'node:path';
import { fileURLToPath } from 'node:url';
import { selectedVirtualUser, type VirtualUserSelection } from '../virtual-user.js';
import { USER_AGENT } from './chat-completions.js';
import {
  RecipeReferenceSchema,
  RSIError,
  TASK_INPUT_FILENAME,
  TaskInputTextSchema,
  type Recipe,
  type RecipeReference,
} from './models.js';

const CLOUD_API_VERSION = '/v1';
export const DEFAULT_CLOUD_BASE_URL = `https://friday-memeory-dev.rosettalab.top${CLOUD_API_VERSION}`;
/** The CoSens gateway accepts request bodies up to 10 MiB. */
export const MAX_CLOUD_REQUEST_BYTES = 10 * 1024 * 1024;
export const MAX_RECIPE_ZIP_BYTES = 8 * 1024 * 1024;

export type CloudOptions = {
  api_key?: string;
  base_url?: string;
  /** Request timeout in seconds (default 60). */
  timeout?: number;
} & VirtualUserSelection;
export type RecipeUpsertOptions = { recipe_id?: string; idempotency_key?: string };
export type RecipeUpsertResult = { recipe_id: string; recipe_version_id: string };

function cloudBase(options: CloudOptions) {
  const base = new URL(options.base_url ?? process.env.COSENS_RSI_BASE_URL ?? DEFAULT_CLOUD_BASE_URL);
  if (!['http:', 'https:'].includes(base.protocol) || base.href !== base.origin + base.pathname)
    throw new TypeError('base_url must be an HTTP(S) address without embedded credentials, query or fragment');
  const prefix = base.pathname.replace(/\/+$/, '');
  base.pathname = prefix.endsWith(CLOUD_API_VERSION) ? prefix : prefix + CLOUD_API_VERSION;
  return base.href;
}

export function recipeCloudDestination(options: CloudOptions, sharedHeaders?: Headers) {
  return {
    base_url: cloudBase(options),
    virtual_user_id: selectedVirtualUser(options) ?? sharedHeaders?.get('X-Virtual-User-Id') ?? null,
  };
}

export type RecipeCloudDestination = ReturnType<typeof recipeCloudDestination>;

// Older journals recorded the configured prefix before the API version was added.
export function recipeCloudBaseAliases(baseUrl: string): readonly [string, string] {
  return [baseUrl, baseUrl.slice(0, -CLOUD_API_VERSION.length)];
}

export function matchesRecipeCloudDestination(recorded: RecipeCloudDestination, current: RecipeCloudDestination) {
  return recipeCloudBaseAliases(current.base_url).includes(recorded.base_url)
    && recorded.virtual_user_id === current.virtual_user_id;
}

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
  const reference = recipeReference === undefined ? undefined : RecipeReferenceSchema.parse(recipeReference);
  if (reference?.recipe_version_id !== undefined)
    throw new TypeError('recipe_reference.recipe_version_id requires local mode');
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
  if (reference) {
    const { path, recipe_id } = reference;
    files.push(await upload(path, { role: 'recipe', recipe_id }));
  }
  return { context: await upload(evidence.context_path), files };
}

function segment(value: string) {
  if (typeof value !== 'string' || !value || value === '.' || value === '..')
    throw new TypeError('ID must be a nonempty string');
  return encodeURIComponent(value);
}

export async function openRecipeZip(path: string | URL) {
  let localPath: string;
  if (path instanceof URL || /^file:/i.test(path)) {
    const uri = new URL(path);
    if (uri.protocol !== 'file:' || (uri.hostname && uri.hostname !== 'localhost') || uri.search || uri.hash)
      throw new TypeError('Recipe file URI must refer to a local path');
    localPath = fileURLToPath(uri);
  } else {
    localPath = path;
  }
  const zip = await openAsBlob(localPath, { type: 'application/zip' });
  if (zip.size === 0 || zip.size > MAX_RECIPE_ZIP_BYTES)
    throw new TypeError('Recipe ZIP must be nonempty and at most 8 MiB');
  return { file: zip, filename: basename(localPath) };
}

export function validateRecipeUpsertOptions(options: RecipeUpsertOptions) {
  if (Object.keys(options).some((key) => key !== 'recipe_id' && key !== 'idempotency_key'))
    throw new TypeError('cloud Recipe upload accepts only recipe_id and idempotency_key options');
  const { recipe_id, idempotency_key } = options;
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
}

export function uploadRecipeZip(
  cloud: CloudClient,
  { file, filename }: { file: Blob; filename: string },
  { recipe_id, idempotency_key }: RecipeUpsertOptions,
): Promise<RecipeUpsertResult> {
  const multipart = new FormData();
  multipart.set('file', file, filename);
  if (recipe_id !== undefined) multipart.set('recipe_id', recipe_id);
  return cloud.request('POST', '/recipes_upsert', { multipart, idempotency_key });
}

/** Cloud mode uploads the supplied evidence and optional Recipe reference to CoSens. */
export class CloudClient {
  readonly #base: string;
  readonly #headers: Headers;
  readonly #timeoutMs: number;

  constructor(options: CloudOptions = {}, sharedHeaders?: Headers) {
    this.#headers = new Headers(sharedHeaders);
    if (this.#headers.keys().next().done) {
      const key = options.api_key ?? process.env.COSENS_API_KEY;
      if (!key)
        throw new TypeError('cloud mode needs COSENS_API_KEY (or api_key)');
      this.#headers.set('Authorization', `Bearer ${key}`);
      this.#headers.set('X-Auth-Type', 'api_key');
    }
    for (const name of ['Content-Type', 'Content-Length', 'Transfer-Encoding', 'Idempotency-Key'])
      this.#headers.delete(name);
    if (!this.#headers.has('User-Agent')) this.#headers.set('User-Agent', USER_AGENT);
    const virtualUserId = selectedVirtualUser(options);
    if (virtualUserId !== undefined) this.#headers.set('X-Virtual-User-Id', virtualUserId);
    const timeout = options.timeout ?? 60;
    if (typeof timeout !== 'number' || !Number.isFinite(timeout) || timeout <= 0)
      throw new TypeError('timeout must be a positive number of seconds');
    this.#timeoutMs = timeout * 1000;
    this.#base = cloudBase(options);
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
    // Backend metadata can contain paths relative to its own /v1 mount.
    const route = path === CLOUD_API_VERSION || path.startsWith(`${CLOUD_API_VERSION}/`)
      ? path.slice(CLOUD_API_VERSION.length) : path;
    const url = new URL(this.#base + route);
    for (const [key, value] of Object.entries(params ?? {}))
      url.searchParams.set(key, value);
    const headers = new Headers(this.#headers);
    if (payload !== undefined) headers.set('Content-Type', 'application/json');
    if (idempotency_key !== undefined) headers.set('Idempotency-Key', idempotency_key);
    let response: Response;
    try {
      response = await fetch(url, {
        method,
        headers,
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
    if (response.status === 422 && code === 'invalid_request') {
      const error = new TypeError(message);
      Object.defineProperty(error, 'status', { value: response.status });
      throw error;
    }
    throw new RSIError(code, message, response.status);
  }

  async execution_create(request: {
    evidence: EvidenceInput & { input_text?: string };
    idempotency_key: string;
    recipe_reference?: RecipeReference;
  }) {
    if (!request?.idempotency_key)
      throw new TypeError('idempotency_key must be a nonempty string');
    const inputText = TaskInputTextSchema.optional().parse(request.evidence?.input_text);
    const evidence = await evidenceBody(request.evidence, request.recipe_reference);
    if (inputText !== undefined) {
      evidence.files.push({
        name: TASK_INPUT_FILENAME,
        content: inputText,
        encoding: 'utf-8',
        role: 'input',
      });
    }
    return this.request('POST', '/executions', {
      body: {
        idempotency_key: request.idempotency_key,
        ...evidence,
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
    if (Object.hasOwn(evidence ?? {}, 'input_text'))
      throw new TypeError('input_text requires execution_create');
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
    const rows: (import('./public-client.js').RecipeListEntry & { status?: unknown })[] = await this.request('GET', '/recipes', {
      params: query ? { recipe_ids: query.recipe_ids.join(',') } : {},
    });
    return rows.map(({ status: _status, ...row }) => row);
  }

  async recipes_upsert(
    path: string | URL,
    options: RecipeUpsertOptions = {},
  ): Promise<RecipeUpsertResult> {
    validateRecipeUpsertOptions(options);
    return uploadRecipeZip(this, await openRecipeZip(path), options);
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
