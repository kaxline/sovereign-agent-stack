/**
 * Container entry point. Listens on :3000 and runs:
 * - the legacy child, the single-budget server the container ran before (credentials in
 *   /app/.env), behind /mcp, /sse and /messages;
 * - one child per connected budget behind /<slug>/mcp, following the rows under
 *   ACTUAL_ROWS_DIR (default /budgets) without a container restart.
 */
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';
import { createProxy } from './proxy.js';
import { Children, readRows, restartDelay, terminate, type ChildHandle } from './rows.js';

const PORT = 3000;
const LEGACY_PORT = 4099;
const POLL_MS = 2_000;
// Reason: Docker sends SIGKILL 10 s after SIGTERM. Row children get 5 s before theirs; the
// legacy child keeps the 8 s it had as PID 1 to drain its Actual connection.
const SHUTDOWN_GRACE_MS = 9_000;
const LEGACY_KILL_AFTER_MS = 8_500;

const {
  values: { 'enable-write': enableWrite },
} = parseArgs({
  options: { 'enable-write': { type: 'boolean', default: false } },
  strict: false,
  allowPositionals: true,
});
const write = enableWrite === true;
const script = fileURLToPath(new URL('../index.js', import.meta.url));
const rowsDir = process.env.ACTUAL_ROWS_DIR || '/budgets';

const log = (line: string): void => {
  process.stderr.write(`${line}\n`);
};

let shuttingDown = false;

// The legacy child runs exactly as the container's entry point did: same cwd, same env.
let legacy: { child: ChildHandle; exited: Promise<void> } | undefined;
let legacyLastRestartAt: number | undefined;

function startLegacy(): void {
  const args = [script, '--sse', '--port', String(LEGACY_PORT)];
  if (write) args.push('--enable-write');
  const child = spawn(process.execPath, args, {
    cwd: '/app',
    env: process.env,
    stdio: ['ignore', 'inherit', 'inherit'],
  });
  const exited = new Promise<void>((resolve) => {
    child.once('exit', (code, signal) => {
      resolve();
      legacy = undefined;
      if (shuttingDown) return;
      const delay = restartDelay(legacyLastRestartAt, Date.now());
      log(
        `actual-mcp launcher: legacy server exited (${signal ?? `code ${code}`}), restarting in ${Math.ceil(delay / 1000)} s`
      );
      setTimeout(() => {
        if (shuttingDown) return;
        legacyLastRestartAt = Date.now();
        startLegacy();
      }, delay);
    });
  });
  legacy = { child, exited };
}

const children = new Children({ script, write, log });

// Reason: the poll runs every 2 s; repeat a skipped row's reason only when it changes.
let lastWarnings = new Set<string>();
function poll(): void {
  const warnings = new Set<string>();
  try {
    const rows = readRows(rowsDir, write, (line) => {
      warnings.add(line);
      if (!lastWarnings.has(line)) log(line);
    });
    children.reconcile(rows);
  } catch (err) {
    log(`actual-mcp launcher: could not read rows: ${(err as NodeJS.ErrnoException).code ?? 'error'}`);
  }
  lastWarnings = warnings;
}

const server = createProxy((slug) => children.lookup(slug), LEGACY_PORT);
server.listen(PORT, () => log(`actual-mcp launcher listening on port ${PORT}`));

startLegacy();
poll();
const pollTimer = setInterval(poll, POLL_MS);

async function shutdown(signal: string): Promise<void> {
  if (shuttingDown) process.exit(1);
  shuttingDown = true;
  log(`actual-mcp launcher: ${signal} received, stopping children`);
  clearInterval(pollTimer);
  server.close();
  server.closeAllConnections();
  const stops = [children.stopAll()];
  if (legacy) stops.push(terminate(legacy.child, legacy.exited, LEGACY_KILL_AFTER_MS));
  const grace = new Promise<void>((resolve) => setTimeout(resolve, SHUTDOWN_GRACE_MS).unref());
  await Promise.race([Promise.all(stops), grace]);
  process.exit(0);
}

for (const signal of ['SIGINT', 'SIGTERM'] as const) {
  process.on(signal, () => void shutdown(signal));
}
