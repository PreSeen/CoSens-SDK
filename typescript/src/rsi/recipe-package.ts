import { constants, closeSync, fstatSync, lstatSync, openSync, readSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { crc32, inflateRawSync } from 'node:zlib';
import { Zip, ZipDeflate } from 'fflate';
import { isMap, parseDocument } from 'yaml';
import { fail, RecipeMetadataSchema, RSIError } from './models.js';

const ENTRYPOINT = 'RECIPE.md';
const MANIFEST = 'manifest.json';
const MAX_ZIP_BYTES = 8 * 1024 * 1024;
const MAX_FILE_BYTES = 4 * 1024 * 1024;
const MAX_UNCOMPRESSED_BYTES = 32 * 1024 * 1024;
const MAX_ENTRIES = 256;
const utf8 = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true });
const cp437 = 'ÇüéâäàåçêëèïîìÄÅÉæÆôöòûùÿÖÜ¢£¥₧ƒáíóúñÑªº¿⌐¬½¼¡«»░▒▓│┤╡╢╖╕╣║╗╝╜╛┐└┴┬├─┼╞╟╚╔╩╦╠═╬╧╨╤╥╙╘╒╓╫╪┘┌█▄▌▐▀αßΓπΣσµτΦΘΩδ∞φε∩≡±≥≤⌠⌡÷≈°∙·√ⁿ²■ ';

function invalid(message: string): never {
  return fail('invalid_argument', message);
}

function caseFoldPath(path: string): string {
  // Case-insensitive NFC paths must agree with the service's Unicode case folding.
  return Array.from(path.normalize('NFC'), (char) => {
    if (char === 'ı') return char;
    if (/[\u13a0-\u13fd\uab70-\uabbf]/u.test(char)) return char.toUpperCase();
    return char.toLowerCase().toUpperCase().toLowerCase();
  }).join('');
}

class PackagePaths {
  readonly #entries = new Map<string, boolean>();

  add(name: string, directory: boolean): void {
    if (/[\\:*?"<>|\x00-\x1f\x7f]/u.test(name) ||
        name.split('/').some((part) => part === '' || part === '.' || part === '..')) {
      invalid('Recipe ZIP paths must be relative POSIX paths');
    }
    const key = caseFoldPath(name) + '/';
    if (this.#entries.has(key)) invalid('Recipe ZIP has duplicate or conflicting paths');
    this.#entries.set(key, directory);
  }

  checkConflicts(): void {
    const paths = [...this.#entries.keys()].sort();
    for (let index = 1; index < paths.length; index++) {
      const previous = paths[index - 1];
      if (!this.#entries.get(previous) && paths[index].startsWith(previous)) {
        invalid('Recipe ZIP has duplicate or conflicting paths');
      }
    }
  }
}

function checkSize(size: number, total: number): void {
  if (size > MAX_FILE_BYTES) invalid('Recipe ZIP file exceeds 4 MiB');
  if (total > MAX_UNCOMPRESSED_BYTES) invalid('Recipe ZIP unpacked contents exceed 32 MiB');
}

function entryName(data: Uint8Array, flags: number): string {
  if (flags & 0x800) return utf8.decode(data);
  return Array.from(data, (byte) => byte < 128 ? String.fromCharCode(byte) : cp437[byte - 128]).join('');
}

function centralDirectoryEnd(data: Buffer): number {
  // The end record is followed by at most 65,535 bytes of ZIP comment.
  for (let offset = data.length - 22; offset >= Math.max(0, data.length - 65_557); offset--) {
    if (data.readUInt32LE(offset) === 0x06054b50 &&
        data.readUInt16LE(offset + 20) === data.length - offset - 22) {
      return offset;
    }
  }
  return invalid('Recipe ZIP could not be read');
}

function readRegularEntries(bytes: Uint8Array): Map<string, Uint8Array> {
  const data = Buffer.from(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const end = centralDirectoryEnd(data);
  const count = data.readUInt16LE(end + 10);
  const start = data.readUInt32LE(end + 16);
  if (data.readUInt32LE(end + 4) !== 0 || data.readUInt16LE(end + 8) !== count ||
      start + data.readUInt32LE(end + 12) !== end) {
    invalid('Recipe ZIP has an invalid central directory');
  }
  if (count > MAX_ENTRIES) invalid('Recipe ZIP exceeds 256 entries');

  const paths = new PackagePaths();
  const files = new Map<string, Uint8Array>();
  const localRanges: { start: number; end: number }[] = [];
  let total = 0;
  let cursor = start;
  for (let index = 0; index < count; index++) {
    if (cursor + 46 > end || data.readUInt32LE(cursor) !== 0x02014b50) {
      invalid('Recipe ZIP has an invalid central directory');
    }
    const flags = data.readUInt16LE(cursor + 8);
    const method = data.readUInt16LE(cursor + 10);
    const checksum = data.readUInt32LE(cursor + 16);
    const compressedSize = data.readUInt32LE(cursor + 20);
    const size = data.readUInt32LE(cursor + 24);
    const nameEnd = cursor + 46 + data.readUInt16LE(cursor + 28);
    const next = nameEnd + data.readUInt16LE(cursor + 30) + data.readUInt16LE(cursor + 32);
    const kind = (data.readUInt32LE(cursor + 38) >>> 16) & 0o170000;
    const local = data.readUInt32LE(cursor + 42);
    if (next > end || data.readUInt16LE(cursor + 34) !== 0 ||
        local + 30 > start || data.readUInt32LE(local) !== 0x04034b50) {
      invalid('Recipe ZIP has an invalid entry');
    }
    const name = entryName(data.subarray(cursor + 46, nameEnd), flags);
    const directory = name.endsWith('/');
    paths.add(directory ? name.slice(0, -1) : name, directory);
    if ((kind !== 0 && kind !== (directory ? 0o040000 : 0o100000)) || (directory && size)) {
      invalid('Recipe ZIP entries must be regular files or directories');
    }
    if (flags & 0x41) invalid('Recipe ZIP cannot contain encrypted entries');
    if (method !== 0 && method !== 8) invalid('Recipe ZIP must use stored or deflated compression');
    total += size;
    checkSize(size, total);

    const localFlags = data.readUInt16LE(local + 6);
    const localNameEnd = local + 30 + data.readUInt16LE(local + 26);
    const contentStart = localNameEnd + data.readUInt16LE(local + 28);
    const contentEnd = contentStart + compressedSize;
    if (contentEnd > start || localFlags & 0x41 || data.readUInt16LE(local + 8) !== method ||
        entryName(data.subarray(local + 30, localNameEnd), localFlags) !== name) {
      invalid('Recipe ZIP has an invalid local entry');
    }
    localRanges.push({ start: local, end: contentEnd });
    if (!directory) {
      const compressed = data.subarray(contentStart, contentEnd);
      const content = method === 0 ? Buffer.from(compressed) : inflateRawSync(compressed, {
        maxOutputLength: Math.max(size, 1),
      });
      if (content.length !== size || crc32(content) !== checksum) {
        invalid('Recipe ZIP entry has an invalid size or checksum');
      }
      files.set(name, content);
    }
    cursor = next;
  }
  paths.checkConflicts();
  localRanges.sort((left, right) => left.start - right.start);
  if (cursor !== end ||
      localRanges.some((range, index) => index > 0 && range.start < localRanges[index - 1].end)) {
    invalid('Recipe ZIP has overlapping or inconsistent entries');
  }
  return files;
}

function validateEntrypoint(bytes: Uint8Array): void {
  let markdown: string;
  try {
    markdown = utf8.decode(bytes);
  } catch {
    return invalid('Recipe requires readable UTF-8 Markdown');
  }
  const header = /^\uFEFF?---[ \t]*\r?\n((?:[^\n]*\n)*?)---[ \t]*(?:\r?\n|$)/u.exec(markdown);
  if (!header) return invalid('Recipe requires YAML frontmatter delimited by ---');
  let metadata: unknown;
  try {
    const document = parseDocument(header[1], { uniqueKeys: true });
    if (document.errors.length || document.warnings.length || !isMap(document.contents)) {
      return invalid('Recipe frontmatter must be a valid YAML mapping');
    }
    metadata = document.toJS();
  } catch {
    return invalid('Recipe frontmatter must be a valid YAML mapping');
  }
  const parsed = RecipeMetadataSchema.safeParse(metadata);
  if (!parsed.success) {
    return invalid('Recipe frontmatter requires nonempty string fields name and description');
  }
  if (Array.from(parsed.data.name).length > 160 || Array.from(parsed.data.description).length > 8000) {
    invalid('Recipe name must be at most 160 characters and description at most 8000');
  }
}

function validateManifest(bytes: Uint8Array, files: Map<string, Uint8Array>): void {
  let manifest: unknown;
  let fields = 0;
  try {
    const text = utf8.decode(bytes);
    manifest = JSON.parse(text);
    // A valid manifest only has scalar/list values. Count root keys before JSON.parse can collapse duplicates.
    let depth = 0;
    for (const [token] of text.matchAll(/"(?:[^"\\]|\\[\s\S])*"|[{}\[\]:]/gu)) {
      if (token === '{' || token === '[') depth++;
      if (token === '}' || token === ']') depth--;
      if (token === ':' && depth === 1) fields++;
    }
  } catch {
    return invalid('manifest.json must be a valid UTF-8 JSON object');
  }
  if (!manifest || typeof manifest !== 'object' || Array.isArray(manifest) || fields !== 2 ||
      Object.keys(manifest).length !== 2 || !('entrypoint' in manifest) || manifest.entrypoint !== ENTRYPOINT ||
      !('files' in manifest) || !Array.isArray(manifest.files) || manifest.files.length !== files.size ||
      new Set(manifest.files).size !== files.size ||
      !manifest.files.every((path: unknown) => typeof path === 'string' && files.has(path))) {
    invalid('manifest.json must list the entrypoint and every Recipe file exactly once');
  }
}

export function readRecipePackage(bytes: Uint8Array): Map<string, Uint8Array> {
  if (bytes.byteLength > MAX_ZIP_BYTES) invalid('Recipe ZIP exceeds 8 MiB');
  let files: Map<string, Uint8Array>;
  try {
    files = readRegularEntries(bytes);
  } catch (error) {
    if (error instanceof RSIError) throw error;
    return invalid('Recipe ZIP could not be read');
  }
  const entrypoint = files.get(ENTRYPOINT);
  const manifest = files.get(MANIFEST);
  if (!entrypoint || !manifest) return invalid('Recipe ZIP requires RECIPE.md and manifest.json at its root');
  files.delete(MANIFEST);
  validateManifest(manifest, files);
  validateEntrypoint(entrypoint);
  return files;
}

function readDirectory(directory: string): Map<string, Uint8Array> {
  if (!lstatSync(directory).isDirectory()) invalid('Recipe version path must be a regular directory');
  const paths = new PackagePaths();
  paths.add(MANIFEST, false);
  const pending = [''];
  const files = new Map<string, Uint8Array>();
  let total = 0;
  while (pending.length) {
    const parent = pending.pop()!;
    for (const rawEntry of readdirSync(join(directory, parent), { encoding: 'buffer' })) {
      const entry = utf8.decode(rawEntry);
      const name = parent ? parent + '/' + entry : entry;
      const path = join(directory, name);
      const info = lstatSync(path);
      paths.add(name, info.isDirectory());
      if (info.isDirectory()) {
        pending.push(name);
        continue;
      }
      if (!info.isFile()) invalid('Recipe version files must be regular files');
      if (files.size + 2 > MAX_ENTRIES) invalid('Recipe ZIP exceeds 256 entries');
      checkSize(info.size, total + info.size);
      const descriptor = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
      try {
        const opened = fstatSync(descriptor);
        if (!opened.isFile() || info.dev !== opened.dev || info.ino !== opened.ino) {
          invalid('Recipe version file changed while packaging');
        }
        checkSize(opened.size, total + opened.size);
        const content = Buffer.alloc(opened.size + 1);
        let length = 0;
        while (length < content.length) {
          const read = readSync(descriptor, content, length, content.length - length, null);
          if (!read) break;
          length += read;
        }
        if (length !== opened.size || fstatSync(descriptor).mtimeMs !== opened.mtimeMs) {
          invalid('Recipe version file changed while packaging');
        }
        total += length;
        files.set(name, content.subarray(0, length));
      } finally {
        closeSync(descriptor);
      }
    }
  }
  paths.checkConflicts();
  if (!files.has(ENTRYPOINT)) invalid('Recipe version requires RECIPE.md at its root');
  return files;
}

export function buildRecipePackage(directory: string): Uint8Array {
  const files = readDirectory(directory);
  validateEntrypoint(files.get(ENTRYPOINT)!);
  const manifest = new TextEncoder().encode(JSON.stringify({
    entrypoint: ENTRYPOINT,
    files: [...files.keys()].sort(),
  }));
  checkSize(manifest.length, manifest.length + [...files.values()].reduce((sum, file) => sum + file.length, 0));
  files.set(MANIFEST, manifest);

  const chunks: Uint8Array[] = [];
  const zip = new Zip((error, chunk) => {
    if (error) throw error;
    chunks.push(chunk);
  });
  for (const name of [...files.keys()].sort()) {
    const entry = new ZipDeflate(name, { level: 6 });
    entry.mtime = new Date(1980, 0, 1);
    entry.os = 3;
    entry.attrs = 0o100644 << 16;
    zip.add(entry);
    entry.push(files.get(name)!, true);
  }
  zip.end();
  const data = Buffer.concat(chunks);
  if (data.byteLength > MAX_ZIP_BYTES) invalid('Recipe ZIP exceeds 8 MiB');
  return data;
}

export function assertRecipePackageMatches(directory: string, bytes: Uint8Array): void {
  const expected = readDirectory(directory);
  const received = readRecipePackage(bytes);
  if (expected.size !== received.size || [...expected].some(([name, content]) => {
    const entry = received.get(name);
    return !entry || Buffer.compare(entry, content) !== 0;
  })) {
    invalid('Recipe ZIP does not match the selected local Recipe version');
  }
}
