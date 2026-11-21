// Type-level checks, compiled by `npm run typecheck` and never run.
// `virtualUserId` is the only Virtual User option; it sets X-Virtual-User-Id.
// `virtual_user_id` is also rejected at runtime (see tests/*.test.ts).
import { RSIClient } from '../src/rsi/index.ts';
import {
  getMemory, getMemoryExperience, getMemoryItems, getMemoryJob, memoryIngest, memoryRecall,
} from '../src/memory.ts';

export function virtualUserHeaderOnly() {
  new RSIClient({ mode: 'cloud', virtualUserId: 'vu_a' });
  memoryRecall({ query: 'q', virtualUserId: 'vu_a' });

  // @ts-expect-error virtual_user_id is not an option
  new RSIClient({ mode: 'cloud', virtual_user_id: 'vu_a' });
  // @ts-expect-error virtual_user_id is not an option
  memoryRecall({ query: 'q', virtual_user_id: 'vu_a' });
  // @ts-expect-error virtual_user_id is not an option
  memoryIngest({ content: 'c', requestKey: 'k', virtual_user_id: 'vu_a' });
  // @ts-expect-error virtual_user_id is not an option
  getMemoryItems({ virtual_user_id: 'vu_a' });
  // @ts-expect-error virtual_user_id is not an option
  getMemoryJob('j', { virtual_user_id: 'vu_a' });
  // @ts-expect-error virtual_user_id is not an option
  getMemory({ virtual_user_id: 'vu_a' });
  // @ts-expect-error virtual_user_id is not an option
  getMemoryExperience('e', { virtual_user_id: 'vu_a' });
}
