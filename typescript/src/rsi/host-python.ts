import { execFile } from 'node:child_process';
import { createHash, randomUUID } from 'node:crypto';
import { constants, createWriteStream, existsSync } from 'node:fs';
import { access, mkdir, mkdtemp, readdir, rename, rm } from 'node:fs/promises';
import { delimiter, isAbsolute, join } from 'node:path';
import { Readable } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import { promisify } from 'node:util';

const exec = promisify(execFile);
const release = '20261124';
const version = '3.12.16';
// Digests from the published python-build-standalone release assets.
const targets: Record<string, [string, string]> = {
  'darwin-arm64': ['aarch64-apple-darwin', '9d6d9d0d85feda3f9cb4eb3e048f0fed21ed280feda087927eaa4fbc7124826f'],
  'darwin-x64': ['x86_64-apple-darwin', '6d7ef4efa1adf65f312e6e865040ce03ef8a4bd7e1b45415193295c9ffc0dbdd'],
  'linux-arm64': ['aarch64-unknown-linux-gnu', '518e24b37e356775d82d5609cd9d48e27e211381a39ae416381ba696bf76c25c'],
  'linux-x64': ['x86_64-unknown-linux-gnu', '7e1adaea26c959d13aa1120be1ae62a1ea1bb52adcb382679fffe9217e22adff'],
  'win32-arm64': ['aarch64-pc-windows-msvc', '005f88ec67b0821df5e90406b561c7698a9fba8387c1e5298a862b6639ad65b9'],
  'win32-x64': ['x86_64-pc-windows-msvc', '96694b50dcfde13d56e40346ddaa552a9cd650290e9b933cecf83c1d82226960'],
};

const probeScript = 'import sys; print(sys.executable if sys.version_info >= (3, 10) else "")';
const processEnv = () => ({
  ...Object.fromEntries(Object.entries(process.env).filter(([key]) => /^(path|systemroot|windir)$/i.test(key))),
  ...(process.platform === 'win32' ? { PYTHON_MANAGER_AUTOMATIC_INSTALL: 'false' } : {}),
});

async function probe(executable: string, args: string[] = []) {
  try {
    const { stdout } = await exec(executable, [...args, '-I', '-B', '-c', probeScript], {
      env: processEnv(),
      timeout: 3_000,
      maxBuffer: 4096,
      windowsHide: true,
    });
    const path = stdout.trim();
    return isAbsolute(path) ? path : undefined;
  } catch {
    return undefined;
  }
}

async function findHostPython(checkAlive: () => void) {
  const env = processEnv();
  const searchPath = Object.entries(env).find(([key]) => key.toLowerCase() === 'path')?.[1] ?? '';
  const candidates = process.platform === 'win32'
    ? [{ name: 'python.exe', args: [] }, { name: 'py.exe', args: ['-3'] }]
    : ['python3', 'python', 'python3.14', 'python3.13', 'python3.12', 'python3.11', 'python3.10']
      .map((name) => ({ name, args: [] }));
  for (const directory of searchPath.split(delimiter).filter(isAbsolute)) {
    if (process.platform === 'win32' && /[\\/]WindowsApps[\\/]?$/i.test(directory)) continue;
    for (const { name, args } of candidates) {
      checkAlive();
      const path = join(directory, name);
      try {
        await access(path, constants.X_OK);
        // Apple's developer-tools shim may display an installation dialog.
        if (process.platform === 'darwin' && path === '/usr/bin/python3') {
          await exec('/usr/bin/xcode-select', ['-p'], { env, timeout: 2_000, maxBuffer: 4096 });
        }
      } catch {
        continue;
      }
      const executable = await probe(path, args);
      if (executable) return executable;
    }
  }
}

async function downloadPython(
  url: string,
  sha256: string,
  destination: string,
  checkAlive: () => void,
) {
  const cancellation = new AbortController();
  const interval = setInterval(() => {
    try { checkAlive(); } catch (error) { cancellation.abort(error); }
  }, 250);
  try {
    const signal = AbortSignal.any([cancellation.signal, AbortSignal.timeout(300_000)]);
    const response = await fetch(url, { signal });
    if (!response.ok || !response.body)
      throw new Error(`Managed Python download failed (HTTP ${response.status}).`);
    const hash = createHash('sha256');
    const reader = response.body.getReader();
    async function* chunks() {
      let size = 0;
      try {
        for (;;) {
          checkAlive();
          const { done, value } = await reader.read();
          if (done) break;
          size += value.byteLength;
          if (size > 96 * 1024 * 1024) throw new Error('Managed Python archive exceeds its size limit.');
          hash.update(value);
          yield value;
        }
      } finally {
        await reader.cancel().catch(() => undefined);
        reader.releaseLock();
      }
    }
    await pipeline(Readable.from(chunks()), createWriteStream(destination, { flags: 'wx', mode: 0o600 }), { signal });
    if (hash.digest('hex') !== sha256)
      throw new Error('Managed Python archive failed its SHA-256 check.');
  } finally {
    clearInterval(interval);
  }
}

export async function resolveHostPython(dataDir: string, checkAlive: () => void): Promise<string> {
  const host = await findHostPython(checkAlive);
  if (host) return host;

  const platform = `${process.platform}-${process.arch}`;
  const artifact = targets[platform];
  if (!artifact) throw new Error(`Managed Python is unavailable for ${platform}.`);
  const runtimes = join(dataDir, 'runtimes');
  const directory = `cpython-${version}-${release}-${platform}`;
  let installed = join(runtimes, directory);
  const relativeExecutable = process.platform === 'win32' ? 'python.exe' : join('bin', 'python3.12');
  const installedExecutable = join(installed, relativeExecutable);
  const cached = await probe(installedExecutable);
  if (cached) return cached;

  await mkdir(runtimes, { recursive: true, mode: 0o700 });
  for (const entry of await readdir(runtimes, { withFileTypes: true })) {
    if (!entry.isDirectory() || !entry.name.startsWith(directory + '-recovered-')) continue;
    checkAlive();
    const recovered = await probe(join(runtimes, entry.name, relativeExecutable));
    if (recovered) return recovered;
  }
  const staging = await mkdtemp(join(runtimes, 'python-download-'));
  try {
    const [target, sha256] = artifact;
    const archive = join(staging, 'python.tar.gz');
    const filename = `cpython-${version}+${release}-${target}-install_only_stripped.tar.gz`;
    const url = `https://github.com/astral-sh/python-build-standalone/releases/download/${release}/${encodeURIComponent(filename)}`;
    await downloadPython(url, sha256, archive, checkAlive);
    checkAlive();
    await exec('tar', ['--no-same-owner', '-xzf', archive, '-C', staging], {
      env: processEnv(),
      timeout: 60_000,
      maxBuffer: 64 * 1024,
      windowsHide: true,
    });
    checkAlive();
    const extracted = join(staging, 'python');
    if (!await probe(join(extracted, relativeExecutable)))
      throw new Error('Managed Python could not start after extraction.');
    try {
      await rename(extracted, installed);
    } catch (error) {
      // Another worker may have completed and published the same runtime.
      const concurrent = await probe(installedExecutable);
      if (concurrent) return concurrent;
      if (!existsSync(installed)) throw error;
      // Leave an invalid old directory alone and publish a fresh copy beside it.
      installed += '-recovered-' + randomUUID();
      await rename(extracted, installed);
    }
    const executable = await probe(join(installed, relativeExecutable));
    if (!executable) throw new Error('Managed Python could not start from its installation directory.');
    return executable;
  } finally {
    await rm(staging, { recursive: true, force: true });
  }
}
