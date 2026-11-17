import { performance } from 'node:perf_hooks';

/**
 * Time source for `Atoms.waitLearning`; tests replace its members. Not part of the public API: the package index does not export it.
 * @internal
 */
export const clock = { now: () => performance.now(), sleep: (ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms)) };
