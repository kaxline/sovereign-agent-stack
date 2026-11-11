/**
 * Container entry point. Listens on :3000 and runs one child per connected budget behind
 * /<slug>/mcp, following the rows under ACTUAL_ROWS_DIR (default /budgets) without a
 * container restart. /mcp, /sse and /messages answer 404: the single-budget server that
 * served them was removed in 0.3.15.
 */
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';
import { createProxy } from './proxy.js';
import { Children, readRows } from './rows.js';

const PORT = 3000;
const POLL_MS = 2_000;
// Reason: Docker sends SIGKILL 10 s after SIGTERM; row children get 5 s before theirs.
const SHUTDOWN_GRACE_MS = 9_000;

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

const server = createProxy((slug) => children.lookup(slug), undefined);
server.listen(PORT, () => log(`actual-mcp launcher listening on port ${PORT}`));

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
  const grace = new Promise<void>((resolve) => setTimeout(resolve, SHUTDOWN_GRACE_MS).unref());
  await Promise.race([Promise.all(stops), grace]);
  process.exit(0);
}

for (const signal of ['SIGINT', 'SIGTERM'] as const) {
  process.on(signal, () => void shutdown(signal));
}
