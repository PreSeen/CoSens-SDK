import { createHash } from 'node:crypto';
import {
  chmodSync,
  mkdirSync,
  readFileSync,
  realpathSync,
  rmSync,
  statSync,
  writeFileSync,
  renameSync,
  readdirSync,
  lstatSync,
} from 'node:fs';
import { join, extname, basename, resolve, sep, isAbsolute } from 'node:path';
import { unzipSync } from 'fflate';
import {
  ManifestSchema,
  fail,
  type MaterialManifest,
  type LocalConfig,
  type EvidenceFile,
} from './models.js';
import { uid, encode } from './store.js';
export const digest = (data: Uint8Array) =>
  createHash('sha256').update(data).digest('hex');
export function snapshot(
  root: string,
  input: MaterialManifest,
  config: LocalConfig,
) {
  const manifest = ManifestSchema.parse(input),
    id = uid('material'),
    directory = join(root, 'materials', id);
  mkdirSync(directory, { recursive: true, mode: 0o700 });
  const refs = [
    {
      path: manifest.context_path,
      role: 'context',
      description: 'Complete host context',
    },
    ...manifest.files,
  ];
  const files: EvidenceFile[] = [];
  let total = 0;
  try {
    for (const [index, ref] of refs.entries()) {
      const path = realpathSync(ref.path);
      const info = statSync(path);
      if (!info.isFile() || info.size > config.max_file_bytes)
        fail(
          'invalid_argument',
          'material must be a regular file within max_file_bytes',
        );
      const data = readFileSync(path);
      total += data.length;
      if (
        data.length > config.max_file_bytes ||
        total > config.max_bundle_bytes
      )
        fail('invalid_argument', 'material bundle exceeds size limit');
      const target = join(
        directory,
        String(index) + extname(path).toLowerCase(),
      );
      writeFileSync(target, data, { mode: 0o400 });
      chmodSync(target, 0o400);
      files.push({
        ...ref,
        path,
        snapshot: target,
        locator: `${id}/${index}`,
        sha256: digest(data),
        size: data.length,
      });
    }
    return {
      id,
      manifest,
      files,
      status: 'ready' as const,
      name: basename(manifest.context_path),
    };
  } catch (error) {
    rmSync(directory, { recursive: true, force: true });
    throw error;
  }
}
export function read_snapshot(file: EvidenceFile) {
  const data = readFileSync(file.snapshot);
  if (digest(data) !== file.sha256)
    fail('invalid_argument', 'evidence snapshot integrity check failed');
  const suffix = extname(file.path).toLowerCase();
  let text: string;
  if (['.docx', '.xlsx'].includes(suffix)) {
    let expanded = 0;
    const entries = unzipSync(data, {
      filter: (f) => {
        const selected =
          suffix === '.docx'
            ? f.name === 'word/document.xml'
            : f.name === 'xl/sharedStrings.xml' ||
              /^xl\/worksheets\/sheet[^/]*\.xml$/.test(f.name);
        if (selected) {
          expanded += f.originalSize;
          if (expanded > 16 * 1024 * 1024)
            fail(
              'invalid_argument',
              'expanded document exceeds extraction limit',
            );
        }
        return selected;
      },
    });
    text = Object.entries(entries)
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([, bytes]) =>
        new TextDecoder('utf-8', { fatal: true })
          .decode(bytes)
          .replace(/<[^>]*>/g, ' ')
          .replace(/&lt;/g, '<')
          .replace(/&gt;/g, '>')
          .replace(/&quot;/g, '"')
          .replace(/&apos;/g, "'")
          .replace(/&amp;/g, '&'),
      )
      .join('\n');
  } else {
    text = new TextDecoder('utf-8', { fatal: true }).decode(data);
    if (text.includes('\0'))
      fail('invalid_argument', 'binary evidence requires a text extraction');
  }
  return {
    text: text.slice(0, 100000),
    truncated: text.length > 100000,
    sha256: file.sha256,
    locator: file.locator,
  };
}
export function atomic_json(path: string, value: unknown) {
  mkdirSync(resolve(path, '..'), { recursive: true, mode: 0o700 });
  const temp = path + '.' + uid('tmp');
  writeFileSync(temp, encode(value), { mode: 0o600 });
  renameSync(temp, path);
}
export function inside(root: string, path: string): string {
  const target = resolve(root, path),
    base = resolve(root);
  if (
    isAbsolute(path) ||
    target === base ||
    !target.startsWith(base + sep) ||
    path.includes('\\')
  )
    fail('invalid_argument', 'path must stay inside the test workspace');
  // Reject all symlink components, including links introduced by another local process.
  let current = base;
  for (const part of target.slice(base.length + 1).split(sep)) {
    current = join(current, part);
    try {
      if (lstatSync(current).isSymbolicLink())
        fail('invalid_argument', 'workspace symlinks are not allowed');
    } catch (e) {
      if ((e as NodeJS.ErrnoException).code !== 'ENOENT') throw e;
    }
  }
  return target;
}
export function workspace_files(root: string): string[] {
  return readdirSync(root, { withFileTypes: true }).flatMap((e) =>
    e.isFile()
      ? [join(root, e.name)]
      : e.isDirectory()
        ? workspace_files(join(root, e.name))
        : [],
  );
}
