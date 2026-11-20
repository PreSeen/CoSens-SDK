import { execFile } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import {
  chmodSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { basename, dirname, join, relative, sep } from 'node:path';
import { promisify } from 'node:util';
import { z } from 'zod';
import { digest, inside, workspace_files } from './materials.js';
import type { EvidenceFile, LocalConfig } from './models.js';

const docker = promisify(execFile);
const imageSchema = z.tuple([
  z.string().regex(/^sha256:[a-f0-9]{64}$/u),
  z.record(z.string(), z.unknown()).nullable(),
]);

const supervisor = String.raw`
import base64, json, os, shutil, signal, stat, subprocess, sys, tempfile
from pathlib import Path

workspace = Path('/workspace')
output = workspace / 'outputs'
max_file, max_bundle = map(int, sys.argv[1:3])

def logs(stream):
    stream.seek(0)
    data = stream.read(16385)
    return data[:16384].decode('utf-8', errors='replace') + ('\n[truncated]' if len(data) > 16384 else '')

def artifacts():
    files, total = {}, 0
    for directory, folders, names in os.walk(output):
        for folder in folders:
            if (Path(directory) / folder).is_symlink():
                raise ValueError('output symlinks are unsupported')
        for name in names:
            file = Path(directory) / name
            path = file.relative_to(output).as_posix()
            info = file.lstat()
            if not stat.S_ISREG(info.st_mode) or '\\' in path or path.split('/')[0] == 'scripts':
                raise ValueError('unsafe output file')
            total += info.st_size
            if info.st_size > max_file or total > max_bundle or len(files) >= 1000:
                raise ValueError('output exceeds the configured limits')
            files[path] = base64.b64encode(file.read_bytes()).decode('ascii')
    return files

receipt = {'exit_code': None, 'stdout': '', 'stderr': '', 'outputs': {}}
try:
    for item in (workspace / 'seed').iterdir():
        target = output / item.name
        if item.is_dir():
            shutil.copytree(item, target)
        else:
            shutil.copyfile(item, target)
    for directory, folders, names in os.walk(output):
        for name in folders + names:
            os.chown(Path(directory) / name, 65534, 65534)
    with tempfile.TemporaryFile(dir='/tmp') as stdout, tempfile.TemporaryFile(dir='/tmp') as stderr:
        try:
            process = subprocess.run([sys.executable, '-I', '-B', str(workspace / sys.argv[3]), *sys.argv[4:]], cwd=output, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, timeout=30, check=False, user=65534, group=65534, extra_groups=())
            exit_code = process.returncode
        except subprocess.TimeoutExpired:
            exit_code = 124
        # Stop any descendant still writing to the isolated output mount.
        try:
            os.kill(-1, signal.SIGKILL)
        except ProcessLookupError:
            pass
        receipt.update(exit_code=exit_code, stdout=logs(stdout), stderr=logs(stderr))
        if exit_code == 124:
            receipt['stderr'] += '\nPython execution timed out'
    if receipt['exit_code'] == 0:
        receipt['outputs'] = artifacts()
except (OSError, ValueError) as error:
    receipt.update(exit_code=None, stderr=str(error), outputs={})
print(json.dumps(receipt))
`;

const receiptSchema = z.object({
  exit_code: z.number().int().nullable(),
  stdout: z.string(),
  stderr: z.string(),
  outputs: z.record(z.string(), z.string()),
});

export type PythonRun = {
  path: string;
  args: string[];
  status: 'completed' | 'unavailable' | 'rejected';
  exit_code: number | null;
  stdout: string;
  stderr: string;
};

export function sandboxInputPath(file: EvidenceFile): string {
  return `${file.locator}/${basename(file.path).replaceAll('\\', '_')}`;
}

function stageFile(root: string, path: string, data: Buffer) {
  const destination = inside(root, path);
  mkdirSync(dirname(destination), { recursive: true });
  for (let directory = dirname(destination); directory !== root; directory = dirname(directory))
    chmodSync(directory, 0o755);
  writeFileSync(destination, data, { mode: 0o644 });
  chmodSync(destination, 0o644);
}

function readOnlyMount(path: string, target: string) {
  const source = `source=${path}`;
  const encoded = /[",]/u.test(source) ? '"' + source.replaceAll('"', '""') + '"' : source;
  return `type=bind,${encoded},target=${target},readonly`;
}

function stageTree(source: string, target: string) {
  for (const file of workspace_files(source)) {
    const path = relative(source, file).split(sep).join('/');
    stageFile(target, path, readFileSync(inside(source, path)));
  }
}

export async function runTestPython(
  path: string,
  args: string[],
  scripts: string,
  workspace: string,
  inputs: EvidenceFile[],
  config: LocalConfig,
  checkAlive: () => void,
): Promise<PythonRun> {
  const unavailable = (stderr: string): PythonRun => ({
    path, args, status: 'unavailable', exit_code: null, stdout: '', stderr,
  });
  const rejected = (stderr: string): PythonRun => ({
    path, args, status: 'rejected', exit_code: null, stdout: '', stderr,
  });
  const { test_python_image: image, max_file_bytes: maxFile, max_bundle_bytes: maxBundle } = config;
  if (!image) return unavailable('Python runner is not configured. Set config.test_python_image to a trusted local Docker image.');
  if (!path.startsWith('scripts/') || !path.endsWith('.py') ||
    path.includes('\\') || path.includes('\0') ||
    path.split('/').some((part) => !part || part === '.' || part === '..') ||
    args.some((arg) => arg.includes('\0'))) {
    return rejected('Provide a Python script path under scripts/ and a list of arguments.');
  }
  let imageId: string;
  try {
    const { stdout } = await docker('docker', [
      'image', 'inspect', '--format', '{{printf "[%q,%s]" .Id (json (index .Config "Volumes"))}}', image,
    ], { timeout: 5_000, maxBuffer: 64 * 1024 });
    const [id, volumes] = imageSchema.parse(JSON.parse(stdout));
    if (volumes && Object.keys(volumes).length)
      return unavailable('The Python image declares volumes. Configure a local image without writable volumes.');
    imageId = id;
  } catch {
    checkAlive();
    return unavailable('Docker could not inspect the configured local Python image.');
  }
  const staging = mkdtempSync(join(tmpdir(), 'cosens-rsi-python-'));
  const name = `cosens-rsi-python-${randomUUID()}`;
  const sandboxInputs = join(staging, 'inputs');
  const sandboxScripts = join(staging, 'scripts');
  const seed = join(staging, 'seed');
  try {
    for (const directory of [sandboxInputs, sandboxScripts, seed]) {
      mkdirSync(directory);
      chmodSync(directory, 0o755);
    }
    stageTree(scripts, sandboxScripts);
    try {
      readFileSync(inside(sandboxScripts, path.slice('scripts/'.length)));
    } catch {
      return rejected('Write the Python script with write_output before running it.');
    }
    stageTree(workspace, seed);
    for (const file of inputs) {
      const data = readFileSync(file.snapshot);
      if (data.length !== file.size || digest(data) !== file.sha256)
        return unavailable('Authorized test input did not pass snapshot integrity verification.');
      stageFile(sandboxInputs, sandboxInputPath(file), data);
    }
    checkAlive();
    let payload: string;
    try {
      ({ stdout: payload } = await docker('docker', [
        'run', '--rm', '--name', name, '--pull=never', '--log-driver', 'none', '--init=false',
        '--network', 'none', '--read-only', '--user', '0:0',
        '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
        '--cap-add', 'SETUID', '--cap-add', 'SETGID', '--cap-add', 'KILL',
        '--cap-add', 'CHOWN', '--cap-add', 'DAC_OVERRIDE',
        '--pids-limit', '64', '--memory', '512m', '--memory-swap', '512m', '--cpus', '1',
        '--mount', readOnlyMount(sandboxInputs, '/workspace/inputs'),
        '--mount', readOnlyMount(sandboxScripts, '/workspace/scripts'),
        '--mount', readOnlyMount(seed, '/workspace/seed'),
        '--tmpfs', `/workspace/outputs:size=${maxBundle},nr_inodes=4096,mode=1777,nodev,nosuid,noexec`,
        '--tmpfs', '/tmp:size=16777216,nr_inodes=128,mode=1777,nodev,nosuid,noexec',
        '--ulimit', `fsize=${maxFile}:${maxFile}`,
        '--workdir', '/workspace/outputs', '--env', 'HOME=/tmp',
        '--entrypoint', 'python3', imageId,
        '-I', '-B', '-c', supervisor, String(maxFile), String(maxBundle), path, ...args,
      ], { timeout: 45_000, maxBuffer: 2 * maxBundle + 1024 * 1024 }));
    } catch (error) {
      await docker('docker', ['rm', '--force', name], { timeout: 5_000 }).catch(() => undefined);
      checkAlive();
      const code = (error as { code?: string | number }).code;
      if (code === 'ENOENT' || code === 125 || code === 126 || code === 127)
        return unavailable('Docker could not launch the configured local Python image.');
      return rejected('The isolated Python runner ended without a valid result.');
    }
    checkAlive();
    const { outputs, ...receipt } = receiptSchema.parse(JSON.parse(payload));
    if (receipt.exit_code === null) return { path, args, status: 'rejected', ...receipt };
    if (receipt.exit_code === 0) {
      let total = 0;
      const artifacts = Object.entries(outputs).map(([path, content]) => {
        const destination = inside(workspace, path);
        const bytes = Buffer.from(content, 'base64');
        total += bytes.length;
        if (path.split('/').some((part) => !part || part === '.' || part === '..') ||
          path.startsWith('scripts/') || path === 'scripts' || bytes.length > maxFile ||
          total > maxBundle || bytes.toString('base64') !== content) {
          throw new Error('Invalid isolated runner output.');
        }
        return { destination, bytes };
      });
      if (artifacts.length > 1000) return rejected('The isolated Python runner produced too many files.');
      for (const entry of readdirSync(workspace))
        rmSync(join(workspace, entry), { recursive: true, force: true });
      for (const { destination, bytes } of artifacts) {
        mkdirSync(dirname(destination), { recursive: true, mode: 0o700 });
        writeFileSync(destination, bytes, { mode: 0o600 });
      }
    }
    return { path, args, status: 'completed', ...receipt };
  } catch {
    checkAlive();
    return rejected('The isolated Python runner could not stage its files or verify the result.');
  } finally {
    rmSync(staging, { recursive: true, force: true });
  }
}
