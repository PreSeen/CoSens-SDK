import assert from 'node:assert/strict';
import { EventEmitter, on as eventsOn } from 'node:events';
import test from 'node:test';
import { DeferredStream } from '../src/deferred-stream.js';

test('emitted listener attaches before the first native stream event', async () => {
  const response = { id: 'response-1', status: 'completed' };
  const native = Object.assign(new EventEmitter(), {
    emitted(event: string) { return new Promise(resolve => native.once(event, resolve)); },
    finalResponse() {
      return new Promise(resolve => queueMicrotask(() => {
        native.emit('response.completed', response);
        resolve(response);
      }));
    },
  });
  const stream = new DeferredStream(async () => native, async () => {}, () => {});
  const event = await Promise.race([
    stream.emitted('response.completed'),
    new Promise((_, reject) => setTimeout(() => reject(Error('first event was missed')), 100)),
  ]);
  assert.equal(event, response);
  assert.equal(await stream.finalResponse(), response);
});

test('emitted preserves preflight error event and rejection semantics', async () => {
  const failure = new Error('recall failed');
  const stream = new DeferredStream(async () => { throw failure; }, async () => {}, () => {});
  const errorEvent = stream.emitted('error');
  const completedEvent = stream.emitted('response.completed');
  assert.equal(await errorEvent, failure);
  await assert.rejects(completedEvent, error => error === failure);
  await assert.rejects(stream.done(), error => error === failure);
});

test('events iterator retains the first native event during deferred startup', async () => {
  const response = { id: 'response-2', status: 'completed' };
  const native = Object.assign(new EventEmitter(), {
    ended: false,
    events(event: string) {
      return native.ended ? (async function* () {})() : eventsOn(native, event, { close: ['end'] });
    },
    finalResponse() {
      return new Promise(resolve => queueMicrotask(() => {
        native.emit('response.completed', response);
        native.ended = true;
        native.emit('end');
        resolve(response);
      }));
    },
  });
  const stream = new DeferredStream(async () => native, async () => {}, () => {});
  const iterator = stream.events('response.completed');
  const collected = await Promise.race([
    (async () => { const result: unknown[] = []; for await (const item of iterator) result.push(item); return result; })(),
    new Promise<never>((_, reject) => setTimeout(() => reject(Error('first iterator event was missed')), 100)),
  ]);
  assert.deepEqual(collected, [[response]]);
});

test('events iterator subscribes before the caller starts consuming it', async () => {
  const response = { id: 'response-3', status: 'completed' };
  let markSubscribed!: () => void;
  const subscribed = new Promise<void>(resolve => { markSubscribed = resolve; });
  let finish!: (response: unknown) => void;
  const final = new Promise(resolve => { finish = resolve; });
  const native = Object.assign(new EventEmitter(), {
    events(event: string) { const iterator = eventsOn(native, event, { close: ['end'] }); markSubscribed(); return iterator; },
    finalResponse() { return final; },
  });
  const stream = new DeferredStream(async () => native, async () => {}, () => {});
  const iterator = stream.events('response.created');
  await subscribed;
  native.emit('response.created', response);
  native.emit('end');
  finish(response);
  const collected: unknown[] = [];
  for await (const item of iterator) collected.push(item);
  assert.deepEqual(collected, [[response]]);
});

test('events(error) yields the native failure without a second rejection', async () => {
  const failure = new Error('native stream failed');
  const native = {
    on() { return this; },
    off() { return this; },
    events() { return (async function* () { yield [failure]; })(); },
    async done() { throw failure; },
    async finalResponse() { throw failure; },
  };
  const stream = new DeferredStream(async () => native, async () => {}, () => {});
  const observed: unknown[] = [];
  for await (const item of stream.events('error')) observed.push(item);
  assert.deepEqual(observed, [[failure]]);
  await assert.rejects(stream.done(), error => error === failure);
});
