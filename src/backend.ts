import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { createInterface } from 'node:readline';
import { delimiter, join } from 'node:path';

export type Result = { ok: boolean; [key: string]: unknown };
type Options = { python: string; project: string; config: string };
const failure = (code: string, message: string, retryable = false): Result => ({
  ok: false, error: { code, message, retryable,
    next_action: retryable ? 'Retry the request after a moment. Pagination may need to restart.' : 'Check the local server setup and restart the MCP connection.' }
});
const retryable = (result: Result) => (result.error as { retryable?: boolean } | undefined)?.retryable === true;

/** One stdio bridge to the shared index owner. Never replay a failed request. */
class WorkerConnection {
  private child: ChildProcessWithoutNullStreams;
  private counter = 0;
  private pending = new Map<number, { resolve: (value: Result) => void; timer: NodeJS.Timeout }>();
  private unavailable = false;
  private stopping: Promise<void> | undefined;
  closed = false;
  lastFailure = failure('BACKEND_UNAVAILABLE', 'The local reader stopped. Retry to start a new worker.', true);

  constructor({ python, project, config }: Options) {
    this.child = spawn(python, ['-m', 'team_drive.worker', '--config', config], {
      cwd: project, windowsHide: true,
      env: { ...process.env, PYTHONPATH: [join(project, 'python'), process.env.PYTHONPATH].filter(Boolean).join(delimiter), PYTHONUNBUFFERED: '1', PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8', PYTHONDONTWRITEBYTECODE: '1' },
      stdio: ['pipe', 'pipe', 'pipe']
    });
    const lines = createInterface({ input: this.child.stdout });
    lines.on('line', line => {
      try {
        if (Buffer.byteLength(line, 'utf8') > 1024 * 1024) throw new Error();
        const reply = JSON.parse(line);
        if (!reply || typeof reply !== 'object') throw new Error();
        if (reply.id === null && reply.result?.ok === false) {
          this.lastFailure = reply.result;
          this.finish();
          return;
        }
        const waiter = this.pending.get(reply.id);
        if (waiter) {
          clearTimeout(waiter.timer);
          this.pending.delete(reply.id);
          waiter.resolve(reply.result && typeof reply.result.ok === 'boolean' ? reply.result : failure('BACKEND_PROTOCOL_ERROR', 'Invalid backend response.'));
        }
      } catch {
        this.lastFailure = failure('BACKEND_PROTOCOL_ERROR', 'Invalid backend response.');
        void this.stop();
      }
    });
    // Do not expose backend tracebacks, configuration paths or source content.
    this.child.stderr.on('data', () => {});
    this.child.on('error', () => {
      this.lastFailure = failure('BACKEND_UNAVAILABLE', 'The local reader could not start. Check Python and setup.');
      this.finish();
    });
    // close follows stdout draining: preserve structured startup errors even on fast exits.
    this.child.on('close', () => { this.closed = true; this.finish(); lines.close(); });
    // EPIPE can race a structured startup error on stdout; drain it before
    // resolving pending requests rather than replacing it with a generic error.
    this.child.stdin.on('error', () => { void this.stop(true); });
  }

  call(method: string, params: Record<string, unknown>): Promise<Result> {
    if (this.unavailable) return Promise.resolve(this.lastFailure);
    if (this.pending.size >= 64) return Promise.resolve(failure('SERVICE_BUSY', 'Too many requests are pending on this connection.', true));
    const timeout = method === 'evidence_bundle' ? 330_000 : 60_000;
    const id = ++this.counter;
    return new Promise(resolveReply => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        resolveReply(failure('REQUEST_TIMEOUT', 'The operation exceeded its time budget. Check progress before retrying; it may still complete.'));
      }, timeout);
      this.pending.set(id, { resolve: resolveReply, timer });
      this.child.stdin.write(JSON.stringify({ id, method, params }) + '\n');
    });
  }

  private finish() {
    this.unavailable = true;
    for (const item of this.pending.values()) { clearTimeout(item.timer); item.resolve(this.lastFailure); }
    this.pending.clear();
  }

  stop(drainOutput = false): Promise<void> {
    if (this.stopping) return this.stopping;
    if (drainOutput) this.unavailable = true;
    else this.finish();
    if (this.closed) return Promise.resolve();
    this.stopping = new Promise(resolveStop => {
      const term = setTimeout(() => this.child.kill('SIGTERM'), 3000);
      const kill = setTimeout(() => this.child.kill('SIGKILL'), 6000);
      term.unref(); kill.unref();
      this.child.once('close', () => { clearTimeout(term); clearTimeout(kill); resolveStop(); });
      // EOF closes SQLite and releases the OS lease on Windows as well as POSIX.
      this.child.stdin.end();
    });
    return this.stopping;
  }
}

/** Keep the MCP transport open across transient capacity/worker failures. */
export class Backend {
  private worker: WorkerConnection;
  private stopped = false;
  private nextStart = 0;
  constructor(private options: Options) { this.worker = new WorkerConnection(options); }
  call(method: string, params: Record<string, unknown>): Promise<Result> {
    if (this.stopped) return Promise.resolve(failure('BACKEND_UNAVAILABLE', 'The MCP connection is closing.'));
    if (this.worker.closed && retryable(this.worker.lastFailure) && Date.now() >= this.nextStart) {
      this.nextStart = Date.now() + 1000;
      this.worker = new WorkerConnection(this.options);
    }
    return this.worker.call(method, params);
  }
  stop(): Promise<void> { this.stopped = true; return this.worker.stop(); }
}
