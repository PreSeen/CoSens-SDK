import { spawn } from 'node:child_process';
import {
  lstatSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  renameSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import { basename, dirname, join, relative, sep } from 'node:path';
import { resolveHostPython } from './host-python.js';
import { digest, inside, workspace_files } from './materials.js';
import type { EvidenceFile, LocalConfig } from './models.js';

export type ScriptRun = {
  path: string;
  args: string[];
  status: 'completed' | 'unavailable' | 'rejected';
  exit_code: number | null;
  stdout: string;
  stderr: string;
};

export function testInputPath(file: EvidenceFile): string {
  return `${file.locator}/${basename(file.path).replaceAll('\\', '_')}`;
}

function stageFile(root: string, path: string, data: Buffer) {
  const target = inside(root, path);
  mkdirSync(dirname(target), { recursive: true, mode: 0o700 });
  writeFileSync(target, data, { mode: 0o600 });
}

function stageTree(source: string, target: string) {
  for (const file of workspace_files(source)) {
    const path = relative(source, file).split(sep).join('/');
    stageFile(target, path, readFileSync(inside(source, path)));
  }
}

function collectOutputs(source: string, target: string, config: LocalConfig) {
  if (!lstatSync(source).isDirectory()) throw new Error('Invalid host output directory.');
  const files = workspace_files(source);
  if (files.length > 1000) throw new Error('Too many host script output files.');
  let total = 0;
  for (const file of files) {
    const relativePath = relative(source, file).split(sep).join('/');
    const validated = inside(source, relativePath);
    if (relativePath.split('/')[0] === 'scripts')
      throw new Error('Host script outputs cannot be placed under scripts/.');
    const size = lstatSync(validated).size;
    if (size > config.max_file_bytes || total + size > config.max_bundle_bytes)
      throw new Error('Host script output exceeds the configured size limit.');
    const bytes = readFileSync(validated);
    total += bytes.length;
    if (bytes.length > config.max_file_bytes || total > config.max_bundle_bytes)
      throw new Error('Host script output exceeds the configured size limit.');
    stageFile(target, relativePath, bytes);
  }
}

function executeHost(
  executable: string, args: string[], cwd: string, env: NodeJS.ProcessEnv,
  checkAlive: () => void,
): Promise<Pick<ScriptRun, 'exit_code' | 'stdout' | 'stderr'>> {
  const useProcessGroup = process.platform !== 'win32';
  const child = spawn(executable, args, {
    cwd, env, detached: useProcessGroup, stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true,
  });
  return new Promise((resolve, reject) => {
    const stdout: Buffer[] = [];
    const stderr: Buffer[] = [];
    let bytes = 0;
    let failure: unknown;
    let timedOut = false;
    const signalProcess = () => {
      try {
        if (useProcessGroup && child.pid) process.kill(-child.pid, 'SIGKILL');
        else child.kill('SIGKILL');
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== 'ESRCH') failure ??= error;
      }
    };
    const stop = () => {
      signalProcess();
      // Windows cannot signal a process group. Closing its pipes also finishes
      // collection if a descendant holds them open after the direct child exits.
      if (!useProcessGroup) {
        child.stdout.destroy();
        child.stderr.destroy();
      }
    };
    const capture = (chunk: Buffer, stream: Buffer[]) => {
      const available = 64 * 1024 - bytes;
      const kept = chunk.subarray(0, available);
      stream.push(kept);
      bytes += kept.length;
      if (kept.length < chunk.length) {
        failure ??= new Error('Host script output exceeds the receipt limit.');
        stop();
      }
    };
    const lease = setInterval(() => {
      try {
        checkAlive();
      } catch (error) {
        failure ??= error;
        stop();
      }
    }, 250);
    const timeout = setTimeout(() => {
      timedOut = true;
      stop();
    }, 30_000);
    child.stdout.on('data', (chunk: Buffer) => capture(chunk, stdout));
    child.stderr.on('data', (chunk: Buffer) => capture(chunk, stderr));
    child.once('error', (error) => { failure ??= error; });
    child.once('exit', () => {
      if (useProcessGroup) signalProcess();
    });
    child.once('close', (code, signal) => {
      clearInterval(lease);
      clearTimeout(timeout);
      if (failure) return reject(failure);
      const errorOutput = Buffer.concat(stderr).toString('utf8');
      resolve({
        exit_code: timedOut ? 124 : code,
        stdout: Buffer.concat(stdout).toString('utf8'),
        stderr: errorOutput + (timedOut ? '\nHost script execution timed out.'
          : signal ? `\nHost script terminated by ${signal}.` : ''),
      });
    });
  });
}

export async function runHostScript(
  language: 'python' | 'typescript',
  path: string,
  args: string[],
  scripts: string,
  workspace: string,
  inputs: EvidenceFile[],
  dataDir: string,
  config: LocalConfig,
  checkAlive: () => void,
): Promise<ScriptRun> {
  const unavailable = (stderr: string): ScriptRun => ({
    path, args, status: 'unavailable', exit_code: null, stdout: '', stderr,
  });
  const rejected = (stderr: string): ScriptRun => ({
    path, args, status: 'rejected', exit_code: null, stdout: '', stderr,
  });
  const extension = language === 'python' ? '.py' : '.ts';
  if (!path.startsWith('scripts/') || !path.endsWith(extension) ||
    path.includes('\\') || path.includes('\0') ||
    path.split('/').some((part) => !part || part === '.' || part === '..') ||
    args.some((arg) => arg.includes('\0'))) {
    return rejected(`Provide a ${extension} script under scripts/ and a list of arguments.`);
  }

  const scriptPath = path.slice('scripts/'.length);
  try {
    if (!lstatSync(inside(scripts, scriptPath)).isFile())
      return rejected('Write the script with write_output before running it.');
  } catch {
    return rejected('Write the script with write_output before running it.');
  }

  let executable: string;
  try {
    checkAlive();
    executable = language === 'python'
      ? await resolveHostPython(dataDir, checkAlive)
      : process.execPath;
  } catch (error) {
    checkAlive();
    return unavailable(error instanceof Error ? error.message : 'Host runtime is unavailable.');
  }

  const staging = mkdtempSync(join(dirname(workspace), '.rsi-host-'));
  const working = join(staging, 'outputs');
  const stagedScripts = join(staging, 'scripts');
  const stagedInputs = join(staging, 'inputs');
  const temporary = join(staging, 'tmp');
  const collected = join(staging, 'collected');
  try {
    for (const directory of [working, stagedScripts, stagedInputs, temporary])
      mkdirSync(directory, { mode: 0o700 });
    stageTree(workspace, working);
    stageTree(scripts, stagedScripts);
    for (const file of inputs) {
      const bytes = readFileSync(file.snapshot);
      if (bytes.length !== file.size || digest(bytes) !== file.sha256)
        return unavailable('Authorized test input did not pass snapshot integrity verification.');
      stageFile(stagedInputs, testInputPath(file), bytes);
    }

    const flags = language === 'python' ? ['-I', '-B']
      : process.versions.bun ? [] : ['--experimental-strip-types'];
    checkAlive();
    let output: Pick<ScriptRun, 'exit_code' | 'stdout' | 'stderr'>;
    try {
      output = await executeHost(
        executable, [...flags, inside(stagedScripts, scriptPath), ...args], working,
        {
          PATH: process.env.PATH,
          SystemRoot: process.env.SystemRoot,
          WINDIR: process.env.WINDIR,
          TMPDIR: temporary,
          TMP: temporary,
          TEMP: temporary,
          HOME: staging,
          USERPROFILE: staging,
        },
        checkAlive,
      );
    } catch (error) {
      checkAlive();
      const code = (error as NodeJS.ErrnoException).code;
      if (code === 'ENOENT' || code === 'EACCES')
        return unavailable('The host runtime could not be launched.');
      return rejected(error instanceof Error ? error.message : 'Host script execution could not complete.');
    }

    checkAlive();
    if (output.exit_code !== 0) {
      return { path, args, status: output.exit_code === null ? 'rejected' : 'completed', ...output };
    }
    mkdirSync(collected, { mode: 0o700 });
    collectOutputs(working, collected, config);
    const previous = join(staging, 'previous-outputs');
    renameSync(workspace, previous);
    try {
      renameSync(collected, workspace);
    } catch (error) {
      renameSync(previous, workspace);
      throw error;
    }
    return { path, args, status: 'completed', ...output };
  } catch {
    checkAlive();
    return rejected('Could not stage or collect host script files in the test workspace.');
  } finally {
    rmSync(staging, { recursive: true, force: true });
  }
}
