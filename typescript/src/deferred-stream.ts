/** Preserve the synchronous event-stream surface while CoSens performs async preflight. */
export class DeferredStream implements AsyncIterable<unknown> {
  private readonly ready: Promise<any>;
  private native: any;
  private readonly listeners: Array<{ event: string; listener: Function; once: boolean }> = [];
  private readonly localController = new AbortController();
  private abortedBeforeStart = false;
  private finalization?: Promise<void>;
  private finalTask?: Promise<void>;
  private startError?: unknown;
  private readonly externalAbort = () => this.abort();

  constructor(start: (signal: AbortSignal) => Promise<any>, private readonly onFinal: (response: any) => Promise<void>, private readonly onIncomplete: () => void, private readonly externalSignal?: AbortSignal) {
    if (externalSignal?.aborted) this.abort();
    else externalSignal?.addEventListener('abort', this.externalAbort, { once: true });
    this.ready = start(this.localController.signal).then(stream => {
      this.native = stream;
      for (const { event, listener, once } of this.listeners) stream[once ? 'once' : 'on']?.(event, listener);
      if (this.abortedBeforeStart) { stream.abort?.(); this.incomplete(); }
      this.finalTask = this.observeFinal(stream);
      void this.finalTask.catch(() => {});
      return stream;
    }).catch(error => {
      this.startError = error;
      this.incomplete();
      this.emitLocal('error', error);
      this.emitLocal('end');
      throw error;
    });
    // The caller may use only event listeners and never await this object.
    void this.ready.catch(() => {});
  }

  private detachExternalSignal() { this.externalSignal?.removeEventListener('abort', this.externalAbort); }
  private incomplete() { this.detachExternalSignal(); this.onIncomplete(); }

  private emitLocal(event: string, ...args: unknown[]) {
    for (const entry of [...this.listeners]) {
      if (entry.event !== event) continue;
      if (entry.once) this.off(event, entry.listener);
      try { entry.listener(...args); } catch { /* preserve the stream's original failure */ }
    }
  }

  private async observeFinal(stream: any) {
    const getter = stream.finalResponse ?? stream.finalChatCompletion;
    if (typeof getter !== 'function') { this.incomplete(); return; }
    try { await this.finalize(await getter.call(stream)); }
    catch (error) { this.incomplete(); throw error; }
    finally { this.detachExternalSignal(); }
  }

  private finalize(response: any): Promise<void> {
    return this.finalization ??= this.onFinal(response).finally(() => this.detachExternalSignal());
  }

  get controller(): AbortController { return this.native?.controller ?? this.localController; }
  get ended(): boolean { return this.native?.ended ?? this.startError !== undefined; }
  get errored(): boolean { return this.native?.errored ?? this.startError !== undefined; }
  get aborted(): boolean { return this.native?.aborted ?? this.abortedBeforeStart; }

  on(event: string, listener: Function): this {
    if (this.native) this.native.on?.(event, listener);
    else this.listeners.push({ event, listener, once: false });
    return this;
  }
  once(event: string, listener: Function): this {
    if (this.native) this.native.once?.(event, listener);
    else this.listeners.push({ event, listener, once: true });
    return this;
  }
  off(event: string, listener: Function): this {
    if (this.native) this.native.off?.(event, listener);
    else {
      const index = this.listeners.findIndex(item => item.event === event && item.listener === listener);
      if (index >= 0) this.listeners.splice(index, 1);
    }
    return this;
  }
  abort(): void {
    this.detachExternalSignal();
    this.abortedBeforeStart = true;
    this.localController.abort();
    if (this.native) { this.native.abort?.(); this.incomplete(); }
  }
  emitted(event: string): Promise<unknown> {
    return new Promise((resolve, reject) => {
      const onError = (error: unknown) => { this.off(event, onEvent); reject(error); };
      const onEvent = (...values: unknown[]) => {
        if (event !== 'error') this.off('error', onError);
        resolve(values.length > 1 ? values : values[0]);
      };
      if (event !== 'error') this.once('error', onError);
      this.once(event, onEvent);
    });
  }
  events(event: string): AsyncIterableIterator<unknown> {
    const early: unknown[][] = [];
    const capture = (...args: unknown[]) => { early.push(args); };
    // Native EventStream.events() subscribes when called, before iteration starts.
    this.on(event, capture);
    const self = this;
    const prepared = this.ready.then(stream => {
      const iterator = stream.events(event);
      this.off(event, capture);
      return { stream, iterator };
    }, error => { this.off(event, capture); throw error; });
    // A caller may create an iterator and never advance it.
    void prepared.catch(() => {});
    return (async function* () {
      let stream: any;
      let drained = false;
      try {
        let iterator: AsyncIterableIterator<unknown>;
        try { ({ stream, iterator } = await prepared); }
        catch (error) {
          if (event !== 'error') throw error;
          for (const item of early.length ? early : [[error]]) yield item;
          drained = true;
          return;
        }
        for (const item of early) yield item;
        yield* iterator;
        drained = true;
        if (event === 'error' || event === 'abort') await self.finalTask?.catch(() => {});
        else await self.finalTask;
      } finally {
        self.off(event, capture);
        if (stream && !drained) { stream.abort?.(); self.incomplete(); }
      }
    })();
  }
  async done(): Promise<void> {
    try { const stream = await this.ready; await stream.done(); await this.finalTask; }
    catch (error) { this.incomplete(); throw error; }
  }
  async finalResponse(): Promise<any> {
    try {
      const stream = await this.ready;
      const response = await (stream.finalResponse ?? stream.finalChatCompletion).call(stream);
      await this.finalize(response);
      return response;
    } catch (error) { this.incomplete(); throw error; }
  }
  async finalChatCompletion(): Promise<any> { return this.finalResponse(); }
  async *[Symbol.asyncIterator](): AsyncIterator<any> {
    const stream = await this.ready;
    let drained = false;
    try {
      for await (const event of stream) yield event;
      drained = true;
      await this.finalTask;
    } finally {
      if (!drained) { stream.abort?.(); this.incomplete(); }
    }
  }
}
