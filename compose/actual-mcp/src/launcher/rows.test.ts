import { EventEmitter } from 'node:events';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Children, readRows, secretValue, type Row, type SpawnFn } from './rows.js';

let dir: string;

function writeRow(slug: string, fields: Record<string, unknown>, token?: string, encryptionPassword?: string): void {
  fs.mkdirSync(path.join(dir, 'instances'), { recursive: true });
  fs.mkdirSync(path.join(dir, 'secrets'), { recursive: true });
  fs.writeFileSync(path.join(dir, 'instances', `${slug}.json`), JSON.stringify(fields));
  if (token !== undefined) fs.writeFileSync(path.join(dir, 'secrets', `${slug}.session_token`), token);
  if (encryptionPassword !== undefined) {
    fs.writeFileSync(path.join(dir, 'secrets', `${slug}.encryption_password`), encryptionPassword);
  }
}

beforeEach(() => {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), 'launcher-rows-'));
});

afterEach(() => {
  fs.rmSync(dir, { recursive: true, force: true });
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe('readRows', () => {
  it('reads every valid row, dropping one trailing newline from secrets', () => {
    writeRow(
      'home',
      { server: 'https://budget.example.com', sync_id: 'sync-home', profile: 'p1', access: 'write' },
      'tok-home\n'
    );
    writeRow('work', { server: 'http://10.0.0.5:5006', sync_id: 'sync-work', profile: 'p2' }, 'tok-work', 'secret\n');
    fs.writeFileSync(path.join(dir, 'secrets', 'server_password'), 'ignored');

    const rows = readRows(dir, false);

    expect(rows.map((r) => r.slug)).toEqual(['home', 'work']);
    expect(rows[0]).toMatchObject({
      slug: 'home',
      serverUrl: 'https://budget.example.com',
      syncId: 'sync-home',
      token: 'tok-home',
    });
    expect(rows[0].encryptionPassword).toBeUndefined();
    expect(rows[1]).toMatchObject({ slug: 'work', token: 'tok-work', encryptionPassword: 'secret' });
    expect(rows[0].fingerprint).not.toBe(rows[1].fingerprint);
  });

  it('keeps spaces in secrets and drops only one trailing \\n or \\r\\n', () => {
    writeRow('crlf', { server: 'local', sync_id: 's1' }, ' tok crlf \r\n', '  enc pw\n\n');
    writeRow('plain', { server: 'local', sync_id: 's2' }, 'tok-plain', ' ');

    const rows = readRows(dir, false);

    expect(rows[0]).toMatchObject({ slug: 'crlf', token: ' tok crlf ', encryptionPassword: '  enc pw\n' });
    expect(rows[1]).toMatchObject({ slug: 'plain', token: 'tok-plain', encryptionPassword: ' ' });
  });

  it('secretValue strips exactly one line ending', () => {
    expect(secretValue('a\n')).toBe('a');
    expect(secretValue('a\r\n')).toBe('a');
    expect(secretValue('a\n\n')).toBe('a\n');
    expect(secretValue(' a ')).toBe(' a ');
    expect(secretValue('a\r')).toBe('a\r');
  });

  it('keeps only write rows when write is true', () => {
    writeRow('home', { server: 'local', sync_id: 's1', profile: 'p1', access: 'write' }, 'tok');
    writeRow('work', { server: 'local', sync_id: 's2', profile: 'p2', access: 'read' }, 'tok');

    expect(readRows(dir, true).map((r) => r.slug)).toEqual(['home']);
  });

  it('maps server "local" to the stack Actual server', () => {
    writeRow('home', { server: 'local', sync_id: 's1', profile: 'p1' }, 'tok');

    expect(readRows(dir, false)[0].serverUrl).toBe('http://actual-server:5006');
  });

  it('returns nothing when the instances directory is missing', () => {
    expect(readRows(path.join(dir, 'absent'), false)).toEqual([]);
  });

  it('skips a row with no token, a bad access value or a bad slug, one stderr line each', () => {
    const lines: string[] = [];
    writeRow('notoken', { server: 'local', sync_id: 's1', profile: 'p1' });
    writeRow('emptytoken', { server: 'local', sync_id: 's1', profile: 'p1' }, ' \n');
    writeRow('admin', { server: 'local', sync_id: 's2', profile: 'p2', access: 'admin' }, 'S3CRET-a');
    writeRow('Bad_Slug', { server: 'local', sync_id: 's3', profile: 'p3' }, 'S3CRET-b');
    writeRow('9lives', { server: 'local', sync_id: 's5', profile: 'p5' }, 'S3CRET-c');
    writeRow('good', { server: 'local', sync_id: 's4', profile: 'p4' }, 'tok-secret-value');

    const rows = readRows(dir, false, (line) => lines.push(line));

    expect(rows.map((r) => r.slug)).toEqual(['good']);
    expect(lines).toHaveLength(5);
    expect(lines.find((l) => l.includes('notoken'))).toMatch(/token/);
    expect(lines.find((l) => l.includes('emptytoken'))).toMatch(/token/);
    expect(lines.find((l) => l.includes('admin'))).toMatch(/access/);
    expect(lines.find((l) => l.includes('Bad_Slug'))).toMatch(/slug/);
    expect(lines.find((l) => l.includes('9lives'))).toMatch(/slug/);
    expect(lines.join('\n')).not.toMatch(/S3CRET|tok-secret-value/);
  });

  it('writes skip lines to stderr by default', () => {
    const write = vi.spyOn(process.stderr, 'write').mockImplementation(() => true);
    writeRow('Bad_Slug', { server: 'local', sync_id: 's3', profile: 'p3' }, 'tok');

    readRows(dir, false);

    expect(write).toHaveBeenCalledTimes(1);
    expect(String(write.mock.calls[0][0])).toContain('Bad_Slug');
  });

  it('changes the fingerprint when a token is rewritten with the same length in the same second', () => {
    writeRow('home', { server: 'local', sync_id: 's1', profile: 'p1' }, 'token-aaaa');
    const tokenFile = path.join(dir, 'secrets', 'home.session_token');
    const stamp = new Date('2026-11-09T12:00:00.000Z');
    fs.utimesSync(tokenFile, stamp, stamp);
    const before = readRows(dir, false)[0].fingerprint;

    fs.writeFileSync(tokenFile, 'token-bbbb');
    fs.utimesSync(tokenFile, stamp, new Date('2026-11-09T12:00:00.400Z'));
    const after = readRows(dir, false)[0].fingerprint;

    expect(after).not.toBe(before);
  });

  it('keeps the fingerprint when nothing changed', () => {
    writeRow('home', { server: 'local', sync_id: 's1', profile: 'p1' }, 'tok');

    expect(readRows(dir, false)[0].fingerprint).toBe(readRows(dir, false)[0].fingerprint);
  });
});

class FakeChild extends EventEmitter {
  pid: number | undefined = 1;
  signals: string[] = [];
  kill(signal?: NodeJS.Signals): boolean {
    this.signals.push(signal ?? 'SIGTERM');
    return true;
  }
  exit(code: number | null, signal: NodeJS.Signals | null = null): void {
    this.emit('exit', code, signal);
  }
}

interface SpawnCall {
  command: string;
  args: string[];
  cwd: string;
  env: Record<string, string | undefined>;
  child: FakeChild;
}

function fakeSpawn(before?: (cwd: string) => void): { spawn: SpawnFn; calls: SpawnCall[] } {
  const calls: SpawnCall[] = [];
  const spawn: SpawnFn = (command, args, options) => {
    before?.(String(options.cwd));
    const child = new FakeChild();
    calls.push({
      command,
      args: [...args],
      cwd: String(options.cwd),
      env: { ...(options.env as Record<string, string | undefined>) },
      child,
    });
    return child;
  };
  return { spawn, calls };
}

function row(slug: string, fingerprint = 'f1', extra: Partial<Row> = {}): Row {
  return {
    slug,
    serverUrl: 'http://actual-server:5006',
    syncId: `sync-${slug}`,
    token: `token-${slug}`,
    fingerprint,
    ...extra,
  };
}

describe('Children', () => {
  let dataRoot: string;
  let cwdRoot: string;

  beforeEach(() => {
    dataRoot = path.join(dir, 'data');
    cwdRoot = path.join(dir, 'cwd');
    process.env.ACTUAL_PASSWORD = 'launcher-password';
  });

  afterEach(() => {
    delete process.env.ACTUAL_PASSWORD;
  });

  function make(
    write = false,
    before?: (cwd: string) => void
  ): { children: Children; calls: SpawnCall[]; lines: string[] } {
    const { spawn, calls } = fakeSpawn(before);
    const lines: string[] = [];
    const children = new Children({
      script: '/app/build/index.js',
      write,
      spawn,
      dataRoot,
      cwdRoot,
      log: (line) => lines.push(line),
    });
    return { children, calls, lines };
  }

  it('spawns a new row once with its own env and an empty cwd', () => {
    const { children, calls } = make(true);
    fs.mkdirSync(path.join(cwdRoot, 'home'), { recursive: true });
    fs.writeFileSync(path.join(cwdRoot, 'home', '.env'), 'ACTUAL_PASSWORD=leak');

    children.reconcile([row('home', 'f1', { encryptionPassword: 'enc' })]);
    children.reconcile([row('home', 'f1', { encryptionPassword: 'enc' })]);

    expect(calls).toHaveLength(1);
    const [call] = calls;
    expect(call.args).toEqual(['/app/build/index.js', '--sse', '--port', '4100', '--enable-write']);
    expect(call.cwd).toBe(path.join(cwdRoot, 'home'));
    expect(call.cwd).not.toBe('/app');
    expect(fs.readdirSync(call.cwd)).toEqual([]);
    expect(call.env).toMatchObject({
      ACTUAL_SERVER_URL: 'http://actual-server:5006',
      ACTUAL_SESSION_TOKEN: 'token-home',
      ACTUAL_BUDGET_SYNC_ID: 'sync-home',
      ACTUAL_BUDGET_ENCRYPTION_PASSWORD: 'enc',
      ACTUAL_DATA_DIR: path.join(dataRoot, 'home'),
      ACTUAL_SYNC_TTL_MS: '0',
      ACTUAL_RECONNECT_HINT: 'reconnect it with addon.sh disconnect actual --instance home, then addon.sh connect',
    });
    expect(call.env.ACTUAL_PASSWORD).toBeUndefined();
    expect(children.lookup('home')).toBe(4100);
  });

  it('leaves out --enable-write and the encryption password when not set', () => {
    const { children, calls } = make(false);

    children.reconcile([row('home'), row('work')]);

    expect(calls[0].args).toEqual(['/app/build/index.js', '--sse', '--port', '4100']);
    expect(calls[1].args).toContain('4101');
    expect('ACTUAL_BUDGET_ENCRYPTION_PASSWORD' in calls[0].env).toBe(false);
  });

  it('kills a removed row and deletes its data dir once it exits', () => {
    vi.useFakeTimers();
    const { children, calls } = make();
    children.reconcile([row('home')]);
    const dataDir = path.join(dataRoot, 'home');
    fs.mkdirSync(dataDir, { recursive: true });

    children.reconcile([]);

    expect(calls[0].child.signals).toEqual(['SIGTERM']);
    expect(children.lookup('home')).toBeUndefined();
    calls[0].child.exit(null, 'SIGTERM');
    expect(fs.existsSync(dataDir)).toBe(false);
    vi.advanceTimersByTime(10_000);
    expect(calls).toHaveLength(1);
    expect(calls[0].child.signals).toEqual(['SIGTERM']);
  });

  it('sends SIGKILL when a child ignores SIGTERM for 5 s', () => {
    vi.useFakeTimers();
    const { children, calls } = make();
    children.reconcile([row('home')]);

    children.reconcile([]);
    vi.advanceTimersByTime(4_999);
    expect(calls[0].child.signals).toEqual(['SIGTERM']);
    vi.advanceTimersByTime(1);
    expect(calls[0].child.signals).toEqual(['SIGTERM', 'SIGKILL']);
  });

  it('kills, deletes and respawns a row whose fingerprint changed', () => {
    vi.useFakeTimers();
    const { children, calls } = make();
    children.reconcile([row('home', 'f1')]);
    const dataDir = path.join(dataRoot, 'home');
    fs.mkdirSync(dataDir, { recursive: true });
    fs.writeFileSync(path.join(dataDir, 'cache'), 'x');

    children.reconcile([row('home', 'f2', { token: 'token-new' })]);

    expect(calls[0].child.signals).toEqual(['SIGTERM']);
    expect(calls).toHaveLength(1);
    calls[0].child.exit(0);
    expect(fs.existsSync(dataDir)).toBe(false);
    expect(calls).toHaveLength(2);
    expect(calls[1].env.ACTUAL_SESSION_TOKEN).toBe('token-new');
    expect(children.lookup('home')).toBeDefined();
  });

  it('restarts a crashed child after 2 s, and waits out the minute on a second crash', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-11-09T12:00:00Z'));
    const { children, calls } = make();
    children.reconcile([row('home')]);

    calls[0].child.exit(1);
    expect(children.lookup('home')).toBeUndefined();
    vi.advanceTimersByTime(1_999);
    expect(calls).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(calls).toHaveLength(2);

    vi.advanceTimersByTime(10_000);
    calls[1].child.exit(1);
    vi.advanceTimersByTime(2_000);
    expect(calls).toHaveLength(2);
    // The first restart was at 12:00:02, so the next may come at 12:01:02.
    vi.advanceTimersByTime(47_999);
    expect(calls).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(calls).toHaveLength(3);
  });

  it('keeps a crashed row dead and deletes its data when it is removed before the restart', () => {
    vi.useFakeTimers();
    const { children, calls } = make();
    children.reconcile([row('home')]);
    fs.mkdirSync(path.join(dataRoot, 'home'), { recursive: true });

    calls[0].child.exit(1);
    children.reconcile([]);
    vi.advanceTimersByTime(120_000);

    expect(calls).toHaveLength(1);
    expect(fs.existsSync(path.join(dataRoot, 'home'))).toBe(false);
  });

  it('deletes data dirs of rows that are gone on the first reconcile', () => {
    const { children } = make();
    fs.mkdirSync(path.join(dataRoot, 'old'), { recursive: true });
    fs.mkdirSync(path.join(dataRoot, 'home'), { recursive: true });

    children.reconcile([row('home')]);

    expect(fs.existsSync(path.join(dataRoot, 'old'))).toBe(false);
    expect(fs.existsSync(path.join(dataRoot, 'home'))).toBe(true);
  });

  it('stopAll terminates every child and keeps their caches', async () => {
    const { children, calls } = make();
    children.reconcile([row('home'), row('work')]);
    fs.mkdirSync(path.join(dataRoot, 'home'), { recursive: true });

    const done = children.stopAll();
    expect(calls.map((c) => c.child.signals)).toEqual([['SIGTERM'], ['SIGTERM']]);
    calls[0].child.exit(0);
    calls[1].child.exit(0);
    await done;

    expect(fs.existsSync(path.join(dataRoot, 'home'))).toBe(true);
    children.reconcile([row('home')]);
    expect(calls).toHaveLength(2);
  });

  it('starts a row whose credentials changed while its crash restart was pending on a fresh cache', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-11-09T12:00:00Z'));
    const { children, calls } = make();
    children.reconcile([row('home', 'f1')]);
    const dataDir = path.join(dataRoot, 'home');
    fs.mkdirSync(dataDir, { recursive: true });
    fs.writeFileSync(path.join(dataDir, 'cache'), 'old');

    calls[0].child.exit(1);
    children.reconcile([row('home', 'f1')]);
    expect(calls).toHaveLength(1);
    expect(fs.existsSync(dataDir)).toBe(true);

    children.reconcile([row('home', 'f2', { token: 'token-new' })]);
    expect(fs.existsSync(dataDir)).toBe(false);
    expect(calls).toHaveLength(2);
    expect(calls[1].env.ACTUAL_SESSION_TOKEN).toBe('token-new');
    vi.advanceTimersByTime(120_000);
    expect(calls).toHaveLength(2);

    // The backoff was reset, so a crash of the new child restarts after 2 s.
    calls[1].child.exit(1);
    vi.advanceTimersByTime(2_000);
    expect(calls).toHaveLength(3);
  });

  it('keeps reconciling other rows when one row has an unreadable token', () => {
    const { children, calls, lines } = make();
    writeRow('keep', { server: 'local', sync_id: 's1', profile: 'p1' }, 'tok-keep');
    writeRow('gone', { server: 'local', sync_id: 's2', profile: 'p2' }, 'tok-gone');
    children.reconcile(readRows(dir, false, () => {}));
    expect(calls.map((c) => c.env.ACTUAL_SESSION_TOKEN)).toEqual(['tok-gone', 'tok-keep']);

    fs.rmSync(path.join(dir, 'instances', 'gone.json'));
    writeRow('broken', { server: 'local', sync_id: 's3', profile: 'p3' });
    fs.mkdirSync(path.join(dir, 'secrets', 'broken.session_token'));
    writeRow('fresh', { server: 'local', sync_id: 's4', profile: 'p4' }, 'tok-fresh');
    const warnings: string[] = [];
    const rows = readRows(dir, false, (line) => warnings.push(line));
    children.reconcile(rows);

    expect(rows.map((r) => r.slug)).toEqual(['fresh', 'keep']);
    expect(warnings).toEqual(['actual-mcp launcher: skipping row broken: cannot read session token: EISDIR']);
    expect(calls[0].child.signals).toEqual(['SIGTERM']);
    expect(calls).toHaveLength(3);
    expect(calls[2].env.ACTUAL_SESSION_TOKEN).toBe('tok-fresh');
    expect(lines.some((l) => l.includes('tok-'))).toBe(false);
  });

  it('survives a spawn that throws: other rows start, the port is reused and the row retries', () => {
    vi.useFakeTimers();
    let fail = true;
    const { children, calls, lines } = make(false, (cwd) => {
      if (fail && cwd.endsWith('/bad')) throw Object.assign(new Error('spawn EAGAIN'), { code: 'EAGAIN' });
    });

    expect(() => children.reconcile([row('bad'), row('good')])).not.toThrow();
    expect(calls.map((c) => c.args[3])).toEqual(['4100']);
    expect(children.lookup('good')).toBe(4100);
    expect(lines.some((l) => l.includes('bad') && l.includes('EAGAIN'))).toBe(true);

    fail = false;
    vi.advanceTimersByTime(2_000);
    expect(calls).toHaveLength(2);
    expect(children.lookup('bad')).toBe(4101);
  });

  it('survives a working directory that cannot be created', () => {
    vi.useFakeTimers();
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(cwdRoot, 'a file, not a directory');
    const { children, calls, lines } = make();

    expect(() => children.reconcile([row('home')])).not.toThrow();
    expect(calls).toHaveLength(0);
    expect(lines.some((l) => l.includes('home') && l.includes('could not start'))).toBe(true);
  });

  it('treats an error event from a process that never started as an exit', () => {
    vi.useFakeTimers();
    const { children, calls } = make();
    children.reconcile([row('home')]);

    calls[0].child.emit('error', Object.assign(new Error('spawn ENOENT'), { code: 'ENOENT' }));
    expect(children.lookup('home')).toBe(4100);
    calls[0].child.pid = undefined;
    calls[0].child.emit('error', Object.assign(new Error('spawn ENOENT'), { code: 'ENOENT' }));
    expect(children.lookup('home')).toBeUndefined();
    vi.advanceTimersByTime(2_000);
    expect(calls).toHaveLength(2);
  });
});
