/**
 * Connected budgets ("rows") and the child MCP server that serves each one.
 *
 * scripts/addon.sh and budget.sh connect write one row per budget:
 * `instances/<slug>.json` plus `secrets/<slug>.session_token` and an optional
 * `secrets/<slug>.encryption_password`. Never log secret values; slugs and reasons only.
 */
import { spawn as nodeSpawn, type SpawnOptions } from 'node:child_process';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

export interface Row {
  slug: string;
  serverUrl: string;
  syncId: string;
  token: string;
  encryptionPassword?: string;
  /** Hash of the row's files (contents and mtimes); a change restarts the child. */
  fingerprint: string;
}

// The slug rule of addon_store.SLUG_RE: a lowercase letter, then up to 30 of a-z, 0-9 and -.
const SLUG_RE = /^[a-z][a-z0-9-]{0,30}$/;
const LOCAL_SERVER_URL = 'http://actual-server:5006';

/**
 * A secret file's value: the text less one trailing newline (\n or \r\n), which an editor or
 * `echo` adds. Other whitespace is part of the value, as budget.sh connect checked it.
 */
export function secretValue(text: string): string {
  if (text.endsWith('\r\n')) return text.slice(0, -2);
  if (text.endsWith('\n')) return text.slice(0, -1);
  return text;
}

const stderrLine = (line: string): void => {
  process.stderr.write(`${line}\n`);
};

const errorCode = (err: unknown): string => (err as NodeJS.ErrnoException)?.code ?? 'error';

/** A row file that exists but can't be read. Carries the error code only, never the message. */
class RowReadError extends Error {
  constructor(
    readonly which: string,
    readonly code: string
  ) {
    super(`cannot read ${which}: ${code}`);
  }
}

/** Reads a file, or undefined when it doesn't exist. */
function readOptional(file: string, which: string): { text: string; mtimeMs: number } | undefined {
  try {
    return { text: fs.readFileSync(file, 'utf8'), mtimeMs: fs.statSync(file).mtimeMs };
  } catch (err) {
    if (errorCode(err) === 'ENOENT') return undefined;
    throw new RowReadError(which, errorCode(err));
  }
}

function isHttpUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return url.protocol === 'http:' || url.protocol === 'https:';
  } catch {
    return false;
  }
}

/**
 * Reads every row under `dir`. With `write`, keeps only rows whose `bind` gives some profile `rw`
 * (the stack's per-instance binding, a map from profile to `ro` or `rw`).
 * A row that can't be served is skipped with one line to `warn` (stderr by default).
 */
export function readRows(dir: string, write: boolean, warn: (line: string) => void = stderrLine): Row[] {
  let names: string[];
  try {
    names = fs.readdirSync(path.join(dir, 'instances'));
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === 'ENOENT') return [];
    throw err;
  }

  const rows: Row[] = [];
  for (const name of names.filter((n) => n.endsWith('.json')).sort()) {
    const slug = name.slice(0, -'.json'.length);
    const skip = (reason: string): void => warn(`actual-mcp launcher: skipping row ${slug}: ${reason}`);
    if (!SLUG_RE.test(slug)) {
      skip('invalid slug');
      continue;
    }
    // Reason: one unreadable row must not stop the others from being reconciled.
    try {
      const row = readRow(dir, slug, write, skip);
      if (row) rows.push(row);
    } catch (err) {
      skip(err instanceof RowReadError ? err.message : `cannot read row: ${errorCode(err)}`);
    }
  }
  return rows;
}

/** Reads one row with a valid slug, or undefined when it is skipped or filtered out. */
function readRow(dir: string, slug: string, write: boolean, skip: (reason: string) => void): Row | undefined {
  const instance = readOptional(path.join(dir, 'instances', `${slug}.json`), 'instance file');
  if (!instance) return undefined;
  let fields: Record<string, unknown>;
  try {
    const parsed: unknown = JSON.parse(instance.text);
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('not an object');
    fields = parsed as Record<string, unknown>;
  } catch {
    skip('instance file is not a JSON object');
    return undefined;
  }

  const bind = fields.bind;
  if (bind !== undefined && (!bind || typeof bind !== 'object' || Array.isArray(bind))) {
    skip('bind must map profiles to ro or rw');
    return undefined;
  }
  const writable = bind !== undefined && Object.values(bind as Record<string, unknown>).includes('rw');
  if (write && !writable) return undefined;

  const server = fields.server;
  const serverUrl = server === 'local' ? LOCAL_SERVER_URL : server;
  if (typeof serverUrl !== 'string' || !isHttpUrl(serverUrl)) {
    skip('server must be an http(s) URL or "local"');
    return undefined;
  }
  if (typeof fields.sync_id !== 'string' || !fields.sync_id.trim()) {
    skip('missing sync_id');
    return undefined;
  }

  const tokenFile = readOptional(path.join(dir, 'secrets', `${slug}.session_token`), 'session token');
  const token = tokenFile ? secretValue(tokenFile.text) : undefined;
  if (!tokenFile || !token?.trim()) {
    skip('missing or empty session token');
    return undefined;
  }
  const passwordFile = readOptional(path.join(dir, 'secrets', `${slug}.encryption_password`), 'encryption password');
  const encryptionPassword = (passwordFile ? secretValue(passwordFile.text) : '') || undefined;

  const hash = createHash('sha256');
  for (const file of [instance, tokenFile, passwordFile]) {
    hash.update(file ? `${file.mtimeMs}\0${file.text.length}\0${file.text}\0` : 'absent\0');
  }

  return {
    slug,
    serverUrl,
    syncId: fields.sync_id.trim(),
    token,
    ...(encryptionPassword ? { encryptionPassword } : {}),
    fingerprint: hash.digest('hex'),
  };
}

/** The part of a ChildProcess the supervisor uses, so tests can pass a fake. */
export interface ChildHandle {
  /** Undefined when the process never started. */
  readonly pid?: number;
  kill(signal?: NodeJS.Signals): boolean;
  once(event: 'exit', listener: (code: number | null, signal: NodeJS.Signals | null) => void): unknown;
  on(event: 'error', listener: (err: Error) => void): unknown;
}

export type SpawnFn = (command: string, args: readonly string[], options: SpawnOptions) => ChildHandle;

export const RESTART_DELAY_MS = 2_000;
export const RESTART_WINDOW_MS = 60_000;
export const KILL_AFTER_MS = 5_000;

/**
 * How long to wait before restarting a child that exited on its own: 2 s, but no
 * sooner than a minute after the previous restart.
 */
export function restartDelay(lastRestartAt: number | undefined, now: number): number {
  if (lastRestartAt === undefined) return RESTART_DELAY_MS;
  return Math.max(RESTART_DELAY_MS, lastRestartAt + RESTART_WINDOW_MS - now);
}

/**
 * Sends SIGTERM, then SIGKILL after `killAfterMs` if the child is still running.
 * `exited` resolves when the child exits; the returned promise resolves then too.
 */
export function terminate(child: ChildHandle, exited: Promise<void>, killAfterMs = KILL_AFTER_MS): Promise<void> {
  child.kill('SIGTERM');
  const timer = setTimeout(() => child.kill('SIGKILL'), killAfterMs);
  child.once('exit', () => clearTimeout(timer));
  return exited.finally(() => clearTimeout(timer));
}

export interface ChildrenOptions {
  /** Absolute path of the server entry point, build/index.js. */
  script: string;
  /** Start children with --enable-write. */
  write: boolean;
  spawn?: SpawnFn;
  /** Parent of each child's ACTUAL_DATA_DIR. */
  dataRoot?: string;
  /** Parent of each child's empty working directory. */
  cwdRoot?: string;
  basePort?: number;
  log?: (line: string) => void;
}

interface Running {
  row: Row;
  port: number;
  child: ChildHandle;
  exited: Promise<void>;
  stopping: boolean;
  /** Delete the data dir after the exit; false only on launcher shutdown. */
  dropCache: boolean;
}

interface Slot {
  running?: Running;
  restartTimer?: NodeJS.Timeout;
  lastRestartAt?: number;
  /** Fingerprint of the row whose child exited on its own; its cache is kept for the restart. */
  crashedFingerprint?: string;
}

/** Starts, stops and restarts one child server per row. */
export class Children {
  private readonly spawn: SpawnFn;
  private readonly dataRoot: string;
  private readonly cwdRoot: string;
  private readonly basePort: number;
  private readonly log: (line: string) => void;
  private readonly slots = new Map<string, Slot>();
  private readonly usedPorts = new Set<number>();
  private desired = new Map<string, Row>();
  private pruned = false;
  private shuttingDown = false;

  constructor(private readonly options: ChildrenOptions) {
    this.spawn = options.spawn ?? ((command, args, opts) => nodeSpawn(command, args, opts));
    this.dataRoot = options.dataRoot ?? '/data/rows';
    this.cwdRoot = options.cwdRoot ?? path.join(os.tmpdir(), 'actual-mcp-rows');
    this.basePort = options.basePort ?? 4100;
    this.log = options.log ?? stderrLine;
  }

  /** The port of a row's running child, if it has one. */
  lookup(slug: string): number | undefined {
    const running = this.slots.get(slug)?.running;
    return running && !running.stopping ? running.port : undefined;
  }

  /** Makes the set of children match `rows`. */
  reconcile(rows: Row[]): void {
    if (this.shuttingDown) return;
    this.desired = new Map(rows.map((r) => [r.slug, r]));
    if (!this.pruned) {
      this.pruned = true;
      this.pruneDataDirs();
    }
    for (const slug of new Set([...this.desired.keys(), ...this.slots.keys()])) {
      this.sync(slug);
    }
  }

  /** Stops every child for launcher shutdown, keeping their caches for the next start. */
  async stopAll(): Promise<void> {
    this.shuttingDown = true;
    const exits: Promise<void>[] = [];
    for (const slot of this.slots.values()) {
      clearTimeout(slot.restartTimer);
      slot.restartTimer = undefined;
      const running = slot.running;
      if (!running) continue;
      if (!running.stopping) {
        running.stopping = true;
        running.dropCache = false;
        exits.push(terminate(running.child, running.exited));
      } else {
        exits.push(running.exited);
      }
    }
    await Promise.all(exits);
  }

  private sync(slug: string): void {
    if (this.shuttingDown) return;
    const slot = this.slots.get(slug) ?? {};
    this.slots.set(slug, slot);
    const want = this.desired.get(slug);
    const running = slot.running;

    if (running) {
      if (running.stopping) return; // the exit handler calls sync again
      if (!want) {
        this.log(`actual-mcp launcher: stopping ${slug} (row removed)`);
        this.stop(running);
      } else if (want.fingerprint !== running.row.fingerprint) {
        this.log(`actual-mcp launcher: restarting ${slug} (row changed)`);
        this.stop(running);
      }
      return;
    }

    if (!want) {
      if (slot.restartTimer) {
        clearTimeout(slot.restartTimer);
        this.removeDataDir(slug);
      }
      this.slots.delete(slug);
      return;
    }
    if (slot.restartTimer) {
      if (want.fingerprint === slot.crashedFingerprint) return;
      // Reason: the kept cache belongs to the old credentials; a new token or password must
      // start on a fresh download, or writes left in the old cache would be stranded.
      this.log(`actual-mcp launcher: restarting ${slug} now (row changed while it was down)`);
      clearTimeout(slot.restartTimer);
      slot.restartTimer = undefined;
      slot.lastRestartAt = undefined;
      this.removeDataDir(slug);
    }
    slot.crashedFingerprint = undefined;
    try {
      this.start(slot, want);
    } catch (err) {
      this.scheduleRestart(slot, want, `could not start: ${errorCode(err)}`);
    }
  }

  /** Restarts a row after 2 s, at most once a minute, keeping its cache. */
  private scheduleRestart(slot: Slot, row: Row, reason: string): void {
    const delay = restartDelay(slot.lastRestartAt, Date.now());
    this.log(`actual-mcp launcher: ${row.slug} ${reason}, restarting in ${Math.ceil(delay / 1000)} s`);
    slot.crashedFingerprint = row.fingerprint;
    slot.restartTimer = setTimeout(() => {
      slot.restartTimer = undefined;
      slot.lastRestartAt = Date.now();
      this.sync(row.slug);
    }, delay);
  }

  private start(slot: Slot, row: Row): void {
    const cwd = path.join(this.cwdRoot, row.slug);
    // Reason: the server loads .env from its cwd; an empty cwd keeps any /app/.env out of a
    // row's child.
    fs.rmSync(cwd, { recursive: true, force: true });
    fs.mkdirSync(cwd, { recursive: true });

    const env: NodeJS.ProcessEnv = {};
    for (const key of ['PATH', 'HOME', 'NODE_ENV', 'TMPDIR']) {
      if (process.env[key]) env[key] = process.env[key];
    }
    Object.assign(env, {
      ACTUAL_SERVER_URL: row.serverUrl,
      ACTUAL_SESSION_TOKEN: row.token,
      ACTUAL_BUDGET_SYNC_ID: row.syncId,
      ACTUAL_DATA_DIR: path.join(this.dataRoot, row.slug),
      ACTUAL_SYNC_TTL_MS: '0',
      ACTUAL_RECONNECT_HINT: `reconnect it with addon.sh disconnect actual --instance ${row.slug}, then addon.sh connect`,
    });
    if (row.encryptionPassword) env.ACTUAL_BUDGET_ENCRYPTION_PASSWORD = row.encryptionPassword;

    const port = this.allocatePort();
    const args = [this.options.script, '--sse', '--port', String(port)];
    if (this.options.write) args.push('--enable-write');

    this.log(`actual-mcp launcher: starting ${row.slug} on port ${port}`);
    let child: ChildHandle;
    try {
      child = this.spawn(process.execPath, args, { cwd, env, stdio: ['ignore', 'inherit', 'inherit'] });
    } catch (err) {
      this.usedPorts.delete(port);
      throw err;
    }
    let markExited: () => void = () => {};
    const exited = new Promise<void>((resolve) => (markExited = resolve));
    const running: Running = { row, port, child, exited, stopping: false, dropCache: true };
    slot.running = running;

    let gone = false;
    const onGone = (how: string): void => {
      if (gone) return;
      gone = true;
      this.usedPorts.delete(port);
      if (slot.running === running) slot.running = undefined;
      markExited();
      if (running.stopping) {
        if (running.dropCache) this.removeDataDir(row.slug);
        this.sync(row.slug);
        return;
      }
      if (this.shuttingDown) return;
      this.scheduleRestart(slot, row, `exited (${how})`);
    };
    child.once('exit', (code, signal) => onGone(signal ?? `code ${code}`));
    // Reason: a failed spawn emits 'error' and no 'exit'; without a listener it would crash
    // the launcher. A child that did start still reports its end through 'exit'.
    child.on('error', (err) => {
      this.log(`actual-mcp launcher: ${row.slug} process error: ${errorCode(err)}`);
      if (child.pid === undefined) onGone(`spawn failed: ${errorCode(err)}`);
    });
  }

  private stop(running: Running): void {
    running.stopping = true;
    void terminate(running.child, running.exited);
  }

  private allocatePort(): number {
    let port = this.basePort;
    while (this.usedPorts.has(port)) port++;
    this.usedPorts.add(port);
    return port;
  }

  private removeDataDir(slug: string): void {
    try {
      fs.rmSync(path.join(this.dataRoot, slug), { recursive: true, force: true });
    } catch (err) {
      this.log(`actual-mcp launcher: could not delete the cache of ${slug}: ${errorCode(err)}`);
    }
  }

  /** Deletes caches left by rows that were removed while the launcher was down. */
  private pruneDataDirs(): void {
    let names: string[];
    try {
      names = fs.readdirSync(this.dataRoot);
    } catch {
      return;
    }
    for (const name of names) {
      if (!this.desired.has(name)) {
        this.log(`actual-mcp launcher: deleting the cache of ${name} (no row)`);
        this.removeDataDir(name);
      }
    }
  }
}
