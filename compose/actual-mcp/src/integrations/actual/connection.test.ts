import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

vi.mock('@actual-app/api', () => ({
  init: vi.fn(),
  getBudgets: vi.fn(),
  downloadBudget: vi.fn(),
  sync: vi.fn(),
  shutdown: vi.fn(),
}));

import * as api from '@actual-app/api';
import { buildInitConfig, getActualConnection, resetActualConnectionForTests } from './connection.js';

/** A promise whose resolve/reject are exposed, for deterministic ordering control. */
function deferred<T>(): { promise: Promise<T>; resolve: (v: T) => void; reject: (e: unknown) => void } {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe('ActualConnection.ensureReady', () => {
  beforeEach(() => {
    resetActualConnectionForTests();
    vi.clearAllMocks();
    vi.mocked(api.init).mockResolvedValue(undefined as never);
    vi.mocked(api.getBudgets).mockResolvedValue([{ id: 'budget-1', cloudFileId: 'cloud-1' }] as never);
    vi.mocked(api.downloadBudget).mockResolvedValue(undefined as never);
    vi.mocked(api.shutdown).mockResolvedValue(undefined as never);
    process.env.ACTUAL_SERVER_URL = 'https://example.invalid';
    process.env.ACTUAL_PASSWORD = 'secret';
    delete process.env.ACTUAL_SYNC_TTL_MS;
  });

  afterEach(() => {
    resetActualConnectionForTests();
  });

  it('calls api.init exactly once for concurrent ensureReady callers', async () => {
    const conn = getActualConnection();
    await Promise.all([conn.ensureReady(), conn.ensureReady(), conn.ensureReady()]);
    expect(api.init).toHaveBeenCalledTimes(1);
    expect(api.downloadBudget).toHaveBeenCalledTimes(1);
  });

  it('does not sticky-cache a failed init: first waiters reject, a later call succeeds', async () => {
    const first = deferred<void>();
    vi.mocked(api.init).mockReturnValueOnce(first.promise as never);
    const conn = getActualConnection();

    const failing = conn.ensureReady();
    first.reject(new Error('init boom'));
    await expect(failing).rejects.toThrow('init boom');

    // api.init resolves normally on the retry (default mockResolvedValue).
    await expect(conn.ensureReady()).resolves.toBeUndefined();
    expect(api.init).toHaveBeenCalledTimes(2);
  });

  it('shuts down the failed attempt before a retry calls api.init again', async () => {
    vi.mocked(api.downloadBudget).mockRejectedValueOnce(new Error('download boom'));
    const conn = getActualConnection();

    await expect(conn.ensureReady()).rejects.toThrow('download boom');
    // api.downloadBudget resolves normally on the retry (default mockResolvedValue).
    await expect(conn.ensureReady()).resolves.toBeUndefined();

    expect(api.init).toHaveBeenCalledTimes(2);
    expect(api.shutdown).toHaveBeenCalledTimes(1);
    // Reason: shutdown from the failed attempt must complete before the retry's
    // api.init() runs, so the module-level API state is never overwritten while
    // the failed attempt's handles are still open.
    expect(vi.mocked(api.shutdown).mock.invocationCallOrder[0]).toBeLessThan(
      vi.mocked(api.init).mock.invocationCallOrder[1]
    );
  });
});

describe('ActualConnection sync-if-stale', () => {
  beforeEach(() => {
    resetActualConnectionForTests();
    vi.clearAllMocks();
    vi.useFakeTimers();
    vi.mocked(api.init).mockResolvedValue(undefined as never);
    vi.mocked(api.getBudgets).mockResolvedValue([{ id: 'budget-1', cloudFileId: 'cloud-1' }] as never);
    vi.mocked(api.downloadBudget).mockResolvedValue(undefined as never);
    vi.mocked(api.sync).mockResolvedValue(undefined as never);
    process.env.ACTUAL_SERVER_URL = 'https://example.invalid';
    process.env.ACTUAL_PASSWORD = 'secret';
    delete process.env.ACTUAL_SYNC_TTL_MS;
  });

  afterEach(() => {
    vi.useRealTimers();
    resetActualConnectionForTests();
  });

  it('does not sync on the first call after init', async () => {
    await getActualConnection().ensureReady();
    expect(api.sync).not.toHaveBeenCalled();
  });

  it('concurrent stale reads share a single sync', async () => {
    const pending = deferred<void>();
    vi.mocked(api.sync).mockReturnValueOnce(pending.promise as never);
    const conn = getActualConnection();

    await conn.ensureReady(); // becomes ready, seeds lastSyncAt
    vi.advanceTimersByTime(61_000); // now stale

    const a = conn.ensureReady();
    const b = conn.ensureReady();
    pending.resolve();
    await Promise.all([a, b]);

    expect(api.sync).toHaveBeenCalledTimes(1);
  });

  it('never syncs when ACTUAL_SYNC_TTL_MS is negative', async () => {
    process.env.ACTUAL_SYNC_TTL_MS = '-1';
    const conn = getActualConnection();
    await conn.ensureReady();
    vi.advanceTimersByTime(600_000);
    await conn.ensureReady();
    expect(api.sync).not.toHaveBeenCalled();
  });

  it('keeps serving when a sync fails', async () => {
    const conn = getActualConnection();
    await conn.ensureReady();
    vi.mocked(api.sync).mockRejectedValueOnce(new Error('server unreachable'));
    vi.advanceTimersByTime(61_000);
    await expect(conn.ensureReady()).resolves.toBeUndefined();
  });
});

/** An error shaped like the ones `api.sync()` throws: a generic message plus a `code` (seen live on 26.11.0). */
function syncError(code: string): Error {
  return Object.assign(new Error('We had an unknown problem opening "budget-1".'), { code });
}

describe('ActualConnection sync failure on an ended session', () => {
  beforeEach(() => {
    resetActualConnectionForTests();
    vi.clearAllMocks();
    vi.useFakeTimers();
    vi.mocked(api.init).mockResolvedValue(undefined as never);
    vi.mocked(api.getBudgets).mockResolvedValue([{ id: 'budget-1', cloudFileId: 'cloud-1' }] as never);
    vi.mocked(api.downloadBudget).mockResolvedValue(undefined as never);
    vi.mocked(api.sync).mockResolvedValue(undefined as never);
    vi.mocked(api.shutdown).mockResolvedValue(undefined as never);
    process.env.ACTUAL_SERVER_URL = 'https://example.invalid';
    process.env.ACTUAL_PASSWORD = 'secret';
    process.env.ACTUAL_SYNC_TTL_MS = '0';
  });

  afterEach(() => {
    vi.useRealTimers();
    delete process.env.ACTUAL_SYNC_TTL_MS;
    resetActualConnectionForTests();
  });

  it('fails the call on token-expired, skips the operation, and re-inits on the next call', async () => {
    const conn = getActualConnection();
    await conn.ensureReady();
    vi.advanceTimersByTime(1);
    vi.mocked(api.sync).mockRejectedValueOnce(syncError('token-expired'));
    const op = vi.fn().mockResolvedValue('written');

    await expect(conn.run(op)).rejects.toThrow(
      'Actual sign-in failed (token-expired). If it keeps failing, ' +
        'reconnect it with make budget-remove and make budget-init (local server) ' +
        'or make budget-add URL=<server> FORCE=1 (remote server).'
    );
    expect(op).not.toHaveBeenCalled();
    expect(api.shutdown).toHaveBeenCalledTimes(1);

    // The next call starts a fresh init instead of answering from the closed copy, and an
    // init that fails the same way keeps the same message.
    vi.mocked(api.init).mockRejectedValueOnce(
      Object.assign(new Error('Authentication failed: invalid or expired session token'), { code: 'token-expired' })
    );
    await expect(conn.run(op)).rejects.toThrow('Actual sign-in failed (token-expired). If it keeps failing');
    expect(api.init).toHaveBeenCalledTimes(2);
    expect(op).not.toHaveBeenCalled();
  });

  it('fails the call on file-access-not-allowed', async () => {
    const conn = getActualConnection();
    await conn.ensureReady();
    vi.advanceTimersByTime(1);
    vi.mocked(api.sync).mockRejectedValueOnce(syncError('file-access-not-allowed'));
    const op = vi.fn().mockResolvedValue('written');

    await expect(conn.run(op)).rejects.toThrow(
      'Access to this budget was removed (file-access-not-allowed); its owner or an admin has to restore it.'
    );
    expect(op).not.toHaveBeenCalled();
    expect(api.shutdown).toHaveBeenCalledTimes(1);
  });

  it.each(['invalid-password', 'unauthorized', 'token-not-found'])('fails the call on %s', async (code) => {
    const conn = getActualConnection();
    await conn.ensureReady();
    vi.advanceTimersByTime(1);
    vi.mocked(api.sync).mockRejectedValueOnce(syncError(code));
    await expect(conn.ensureReady()).rejects.toThrow(`Actual sign-in failed (${code})`);
  });

  it('fails the call on file-not-found (budget deleted on the server)', async () => {
    const conn = getActualConnection();
    await conn.ensureReady();
    vi.advanceTimersByTime(1);
    vi.mocked(api.sync).mockRejectedValueOnce(syncError('file-not-found'));
    const op = vi.fn();

    await expect(conn.run(op)).rejects.toThrow(
      'This budget was deleted on the server (file-not-found); reconnect it with make budget-remove and ' +
        'make budget-init (local server) or make budget-add URL=<server> FORCE=1 (remote server) with another budget.'
    );
    expect(op).not.toHaveBeenCalled();
    expect(api.shutdown).toHaveBeenCalledTimes(1);
  });

  it.each(['file-needs-upload', 'file-has-reset', 'file-has-new-key', 'file-key-mismatch', 'file-old-version'])(
    'fails the call on %s',
    async (code) => {
      const conn = getActualConnection();
      await conn.ensureReady();
      vi.advanceTimersByTime(1);
      vi.mocked(api.sync).mockRejectedValueOnce(syncError(code));
      const op = vi.fn();

      await expect(conn.run(op)).rejects.toThrow(
        `The budget changed on the server (${code}); reconnect it with make budget-remove and make budget-init`
      );
      expect(op).not.toHaveBeenCalled();
      expect(api.shutdown).toHaveBeenCalledTimes(1);
    }
  );

  it('uses ACTUAL_RECONNECT_HINT in place of the default hint', async () => {
    process.env.ACTUAL_RECONNECT_HINT = 'run budget.sh connect --slug home --force';
    try {
      const conn = getActualConnection();
      await conn.ensureReady();
      vi.advanceTimersByTime(1);
      vi.mocked(api.sync).mockRejectedValueOnce(syncError('file-has-reset'));
      await expect(conn.ensureReady()).rejects.toThrow(
        /^The budget changed on the server \(file-has-reset\); run budget\.sh connect --slug home --force\.$/
      );
    } finally {
      delete process.env.ACTUAL_RECONNECT_HINT;
    }
  });

  it('wraps an init that cannot find the budget in a stable message', async () => {
    vi.mocked(api.downloadBudget).mockRejectedValue(
      Object.assign(new Error('Budget "cloud-1" not found. Check the sync id.'), { code: 'budget-not-found' })
    );
    const conn = getActualConnection();
    const expected = "This budget isn't on the server or this sign-in can't open it (budget-not-found);";
    await expect(conn.ensureReady()).rejects.toThrow(expected);
    await expect(conn.ensureReady()).rejects.toThrow(expected);
    expect(api.init).toHaveBeenCalledTimes(2);
  });

  it('leaves an init error with another code unwrapped', async () => {
    vi.mocked(api.init).mockRejectedValueOnce(
      Object.assign(new Error('Authentication failed: server offline or unreachable'), { code: 'network-failure' })
    );
    await expect(getActualConnection().ensureReady()).rejects.toThrow(
      'Authentication failed: server offline or unreachable'
    );
  });

  it('queues a caller that arrives during the shutdown behind it', async () => {
    const shutdown = deferred<void>();
    const conn = getActualConnection();
    await conn.ensureReady();
    vi.advanceTimersByTime(1);
    vi.mocked(api.sync).mockRejectedValueOnce(syncError('token-expired'));
    vi.mocked(api.shutdown).mockReturnValueOnce(shutdown.promise as never);

    const first = conn.run(vi.fn());
    const firstSettled = first.catch((e: unknown) => e);
    // Let the sync reject and closeAfterEndedSession() reach api.shutdown().
    while (vi.mocked(api.shutdown).mock.calls.length === 0) await Promise.resolve();

    const late = vi.fn().mockResolvedValue('fresh');
    const second = conn.run(late);
    for (let i = 0; i < 20; i++) await Promise.resolve();
    expect(api.init).toHaveBeenCalledTimes(1); // the fresh init waits for the shutdown
    expect(late).not.toHaveBeenCalled();

    shutdown.resolve();
    expect(String(await firstSettled)).toContain('Actual sign-in failed (token-expired)');
    await expect(second).resolves.toBe('fresh');
    expect(api.init).toHaveBeenCalledTimes(2);
    expect(vi.mocked(api.shutdown).mock.invocationCallOrder[0]).toBeLessThan(
      vi.mocked(api.init).mock.invocationCallOrder[1]
    );
  });

  it('shuts down once when drainAndClose() races the ended-session shutdown', async () => {
    const shutdown = deferred<void>();
    const conn = getActualConnection();
    await conn.ensureReady();
    vi.advanceTimersByTime(1);
    vi.mocked(api.sync).mockRejectedValueOnce(syncError('token-expired'));
    vi.mocked(api.shutdown).mockReturnValueOnce(shutdown.promise as never);

    const call = conn.run(vi.fn()).catch((e: unknown) => e);
    while (vi.mocked(api.shutdown).mock.calls.length === 0) await Promise.resolve();

    const closed = conn.drainAndClose();
    shutdown.resolve();
    await closed;

    expect(String(await call)).toContain('Actual sign-in failed (token-expired)');
    expect(api.shutdown).toHaveBeenCalledTimes(1);
    await expect(conn.run(vi.fn())).rejects.toThrow('ActualConnection is closed');
  });

  it('fails every caller sharing the failed sync and shuts down once', async () => {
    const pending = deferred<void>();
    const conn = getActualConnection();
    await conn.ensureReady();
    vi.advanceTimersByTime(1);
    vi.mocked(api.sync).mockReturnValueOnce(pending.promise as never);
    const a = conn.run(vi.fn());
    const b = conn.run(vi.fn());
    pending.reject(syncError('token-expired'));
    await expect(a).rejects.toThrow('Actual sign-in failed (token-expired)');
    await expect(b).rejects.toThrow('Actual sign-in failed (token-expired)');
    expect(api.shutdown).toHaveBeenCalledTimes(1);
  });

  it.each(['network-failure', 'network', 'internal'])('keeps serving local data on %s', async (code) => {
    const conn = getActualConnection();
    await conn.ensureReady();
    vi.advanceTimersByTime(1);
    vi.mocked(api.sync).mockRejectedValueOnce(syncError(code));
    const op = vi.fn().mockResolvedValue('local');

    await expect(conn.run(op)).resolves.toBe('local');
    expect(api.shutdown).not.toHaveBeenCalled();
    expect(api.init).toHaveBeenCalledTimes(1);
  });

  it('does not match on message text: an uncoded error that mentions a session keeps serving', async () => {
    const conn = getActualConnection();
    await conn.ensureReady();
    vi.advanceTimersByTime(1);
    vi.mocked(api.sync).mockRejectedValueOnce(new Error('token-expired'));
    await expect(conn.ensureReady()).resolves.toBeUndefined();
  });
});

describe('ActualConnection.run', () => {
  beforeEach(() => {
    resetActualConnectionForTests();
    vi.clearAllMocks();
    vi.mocked(api.init).mockResolvedValue(undefined as never);
    vi.mocked(api.getBudgets).mockResolvedValue([{ id: 'budget-1', cloudFileId: 'cloud-1' }] as never);
    vi.mocked(api.downloadBudget).mockResolvedValue(undefined as never);
    process.env.ACTUAL_SERVER_URL = 'https://example.invalid';
    process.env.ACTUAL_PASSWORD = 'secret';
    delete process.env.ACTUAL_SYNC_TTL_MS;
  });

  afterEach(() => resetActualConnectionForTests());

  it('serializes operations: the second starts only after the first finishes', async () => {
    const conn = getActualConnection();
    const events: string[] = [];
    const first = deferred<void>();
    const firstStarted = deferred<void>();

    const p1 = conn.run(async () => {
      events.push('start-1');
      firstStarted.resolve();
      await first.promise;
      events.push('end-1');
    });
    const p2 = conn.run(async () => {
      events.push('start-2');
    });

    await firstStarted.promise;
    expect(events).toEqual(['start-1']);

    first.resolve();
    await Promise.all([p1, p2]);
    expect(events).toEqual(['start-1', 'end-1', 'start-2']);
  });

  it('initializes exactly once across overlapping operations', async () => {
    const conn = getActualConnection();
    await Promise.all([conn.run(async () => 'a'), conn.run(async () => 'b')]);
    expect(api.init).toHaveBeenCalledTimes(1);
  });

  it('propagates operation results and errors without breaking the queue', async () => {
    const conn = getActualConnection();
    await expect(conn.run(async () => 42)).resolves.toBe(42);
    await expect(conn.run(async () => Promise.reject(new Error('op boom')))).rejects.toThrow('op boom');
    await expect(conn.run(async () => 'ok')).resolves.toBe('ok');
  });
});

describe('ActualConnection.drainAndClose', () => {
  beforeEach(() => {
    resetActualConnectionForTests();
    vi.clearAllMocks();
    vi.mocked(api.init).mockResolvedValue(undefined as never);
    vi.mocked(api.getBudgets).mockResolvedValue([{ id: 'budget-1', cloudFileId: 'cloud-1' }] as never);
    vi.mocked(api.downloadBudget).mockResolvedValue(undefined as never);
    vi.mocked(api.shutdown).mockResolvedValue(undefined as never);
    process.env.ACTUAL_SERVER_URL = 'https://example.invalid';
    process.env.ACTUAL_PASSWORD = 'secret';
    delete process.env.ACTUAL_SYNC_TTL_MS;
  });

  afterEach(() => resetActualConnectionForTests());

  it('awaits an in-flight operation before calling api.shutdown', async () => {
    const conn = getActualConnection();
    const gate = deferred<void>();
    const opStarted = deferred<void>();
    let opFinished = false;

    const opPromise = conn.run(async () => {
      opStarted.resolve();
      await gate.promise;
      opFinished = true;
    });

    await opStarted.promise;
    const closePromise = conn.drainAndClose();
    // shutdown must not happen while the op is still running.
    await Promise.resolve();
    expect(api.shutdown).not.toHaveBeenCalled();
    expect(opFinished).toBe(false);

    gate.resolve();
    await opPromise;
    await closePromise;
    expect(opFinished).toBe(true);
    expect(api.shutdown).toHaveBeenCalledTimes(1);
  });

  it('waits for direct in-flight initialization before shutting down', async () => {
    const initGate = deferred<void>();
    vi.mocked(api.init).mockReturnValueOnce(initGate.promise as never);
    const conn = getActualConnection();

    const readyPromise = conn.ensureReady();
    const closePromise = conn.drainAndClose();
    await Promise.resolve();
    expect(api.shutdown).not.toHaveBeenCalled();

    initGate.resolve();
    await readyPromise;
    await closePromise;
    expect(api.shutdown).toHaveBeenCalledTimes(1);
  });

  it('waits for direct in-flight synchronization before shutting down', async () => {
    const conn = getActualConnection();
    await conn.ensureReady();
    const syncGate = deferred<void>();
    process.env.ACTUAL_SYNC_TTL_MS = '0';
    vi.mocked(api.sync).mockReturnValueOnce(syncGate.promise as never);

    const syncPromise = conn.ensureReady();
    const closePromise = conn.drainAndClose();
    await Promise.resolve();
    expect(api.shutdown).not.toHaveBeenCalled();

    syncGate.resolve();
    await syncPromise;
    await closePromise;
    expect(api.shutdown).toHaveBeenCalledTimes(1);
  });

  it('rejects operations submitted after close', async () => {
    const conn = getActualConnection();
    await conn.ensureReady();
    await conn.drainAndClose();
    await expect(conn.run(async () => 'nope')).rejects.toThrow(/closed/);
    await expect(conn.ensureReady()).rejects.toThrow(/closed/);
  });

  it('is idempotent and shuts down exactly once', async () => {
    const conn = getActualConnection();
    await conn.ensureReady();
    await Promise.all([conn.drainAndClose(), conn.drainAndClose()]);
    expect(api.shutdown).toHaveBeenCalledTimes(1);
  });

  it('closes cleanly when never initialized (no api.shutdown)', async () => {
    const conn = getActualConnection();
    await conn.drainAndClose();
    expect(api.shutdown).not.toHaveBeenCalled();
  });

  it('still shuts the API down when init opened a handle but a later step failed', async () => {
    vi.mocked(api.getBudgets).mockResolvedValue([] as never);
    const conn = getActualConnection();
    await expect(conn.ensureReady()).rejects.toThrow(/No budgets found/);

    await conn.drainAndClose();
    expect(api.shutdown).toHaveBeenCalledTimes(1);
  });

  it('logs a shutdown failure and still reaches the closed state', async () => {
    const conn = getActualConnection();
    await conn.ensureReady();
    const shutdownError = new Error('shutdown boom');
    vi.mocked(api.shutdown).mockRejectedValueOnce(shutdownError as never);
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined);

    try {
      await expect(conn.drainAndClose()).resolves.toBeUndefined();
      expect(consoleError).toHaveBeenCalledWith('Error shutting down Actual Budget API:', shutdownError);
      expect((conn as unknown as { state: string }).state).toBe('closed');
      await expect(conn.ensureReady()).rejects.toThrow(/closed/);
    } finally {
      consoleError.mockRestore();
    }
  });
});

describe('buildInitConfig', () => {
  const saved = { ...process.env };

  beforeEach(() => {
    delete process.env.ACTUAL_SERVER_URL;
    delete process.env.ACTUAL_PASSWORD;
    delete process.env.ACTUAL_SESSION_TOKEN;
    delete process.env.ACTUAL_VERBOSE;
  });

  afterEach(() => {
    process.env = { ...saved };
    vi.restoreAllMocks();
  });

  it('uses the session token when ACTUAL_SESSION_TOKEN is set', () => {
    process.env.ACTUAL_SERVER_URL = 'https://example.invalid';
    process.env.ACTUAL_SESSION_TOKEN = 'token-123';
    expect(buildInitConfig('/data')).toEqual({
      dataDir: '/data',
      serverURL: 'https://example.invalid',
      sessionToken: 'token-123',
      verbose: false,
    });
  });

  it('prefers the session token over the password and warns', () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    process.env.ACTUAL_SERVER_URL = 'https://example.invalid';
    process.env.ACTUAL_SESSION_TOKEN = 'token-123';
    process.env.ACTUAL_PASSWORD = 'secret';
    expect(buildInitConfig('/data')).toEqual({
      dataDir: '/data',
      serverURL: 'https://example.invalid',
      sessionToken: 'token-123',
      verbose: false,
    });
    expect(consoleError).toHaveBeenCalledOnce();
  });

  it('falls back to password auth when no session token is set', () => {
    process.env.ACTUAL_SERVER_URL = 'https://example.invalid';
    process.env.ACTUAL_PASSWORD = 'secret';
    expect(buildInitConfig('/data')).toEqual({
      dataDir: '/data',
      serverURL: 'https://example.invalid',
      password: 'secret',
      verbose: false,
    });
  });

  it('turns loot-core verbose logging on only when ACTUAL_VERBOSE=true', () => {
    process.env.ACTUAL_VERBOSE = 'true';
    expect(buildInitConfig('/data')).toEqual({ dataDir: '/data', verbose: true });
  });

  it('ignores credentials when no server URL is set', () => {
    process.env.ACTUAL_SESSION_TOKEN = 'token-123';
    process.env.ACTUAL_PASSWORD = 'secret';
    expect(buildInitConfig('/data')).toEqual({ dataDir: '/data', verbose: false });
  });
});

describe('ActualConnection auth failures', () => {
  beforeEach(() => {
    resetActualConnectionForTests();
    vi.clearAllMocks();
    process.env.ACTUAL_SERVER_URL = 'https://example.invalid';
    process.env.ACTUAL_SESSION_TOKEN = 'expired-token';
    delete process.env.ACTUAL_PASSWORD;
  });

  afterEach(() => {
    delete process.env.ACTUAL_SESSION_TOKEN;
    resetActualConnectionForTests();
  });

  it('surfaces an expired session token error from api.init', async () => {
    vi.mocked(api.init).mockRejectedValueOnce(new Error('Authentication failed: invalid or expired session token'));
    await expect(getActualConnection().ensureReady()).rejects.toThrow('expired session token');
    expect(api.init).toHaveBeenCalledWith(expect.objectContaining({ sessionToken: 'expired-token' }));
  });
});
