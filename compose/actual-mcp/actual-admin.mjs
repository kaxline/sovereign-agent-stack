// Admin commands for scripts/budget.sh, run in the actual-mcp image:
//   docker compose run --rm --no-deps -T -e ACTUAL_SERVER_URL -e ACTUAL_PASSWORD ... \
//     --entrypoint node actual-mcp-rw /opt/actual-admin.mjs <command>
//
// Sign-in uses ACTUAL_SESSION_TOKEN when it is set and ACTUAL_PASSWORD otherwise.
//
//   check          Open ACTUAL_BUDGET_SYNC_ID. Exit 0 when it opens. Otherwise
//                  print one word to stdout and exit with its code:
//                    encryption  2  wrong or missing encryption password
//                    login       3  the server refused the password or token
//                    timeout     4  (see below)
//                    no_access   5  the server has no such budget for this login
//                    unreachable 6  the server can't be reached
//                  Anything else exits 1 with no word.
//   create <name>  Create a budget on the server and print its sync id.
//   list           One "<sync id>\t<name>" line per budget that isn't deleted.
//
// Every command gives up after ACTUAL_ADMIN_TIMEOUT_S seconds (default 45),
// prints "timeout" and exits 4, so a server that never answers can't hang
// make doctor or budget.sh. A node timer, since macOS has no timeout(1).
//
// Credentials come from the environment only (ACTUAL_SERVER_URL,
// ACTUAL_PASSWORD, ACTUAL_SESSION_TOKEN, ACTUAL_BUDGET_SYNC_ID, ACTUAL_BUDGET_ENCRYPTION_PASSWORD) and
// are never printed: the API's own logging is silenced and error messages are
// scrubbed of them before they reach stderr.
import { mkdtempSync, rmSync } from 'node:fs';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const require = createRequire('/app/package.json');
const api = require('@actual-app/api');

const env = process.env;
const secrets = [env.ACTUAL_PASSWORD, env.ACTUAL_SESSION_TOKEN, env.ACTUAL_BUDGET_ENCRYPTION_PASSWORD].filter(Boolean);
const out = (s) => process.stdout.write(`${s}\n`);
const scrub = (s) => secrets.reduce((acc, v) => acc.split(v).join('***'), String(s));
const err = (s) => process.stderr.write(`${scrub(s)}\n`);

// The API logs freely (sync details, full error objects). Keep it off both streams.
for (const k of ['log', 'info', 'warn', 'error', 'debug']) console[k] = () => {};

const LOGIN = new Set(['invalid-password', 'unauthorized', 'token-expired', 'token-not-found']);
const ENCRYPTION = new Set(['missing-key', 'decrypt-failure']);

class Exit extends Error {
  constructor(code, word, msg) {
    super(msg);
    this.exitCode = code;
    this.word = word;
  }
}

let dataDir;

// Maps an API error to an Exit; anything unrecognised is returned unchanged.
function classify(e) {
  if (LOGIN.has(e?.code)) return new Exit(3, 'login', 'the server refused the password or token');
  if (ENCRYPTION.has(e?.code)) return new Exit(2, 'encryption', 'wrong or missing encryption password');
  if (e?.code === 'budget-not-found') return new Exit(5, 'no_access', 'the server has no such budget for this login');
  if (e?.code === 'network-failure') return new Exit(6, 'unreachable', 'the server can not be reached');
  return e;
}

async function open() {
  if (!env.ACTUAL_SERVER_URL) throw new Exit(1, '', 'ACTUAL_SERVER_URL is not set');
  if (!env.ACTUAL_SESSION_TOKEN && !env.ACTUAL_PASSWORD) {
    throw new Exit(3, 'login', 'neither ACTUAL_SESSION_TOKEN nor ACTUAL_PASSWORD is set');
  }
  // A scratch cache, so admin runs never touch the MCP servers' /data copy.
  dataDir = mkdtempSync(join(tmpdir(), 'actual-admin-'));
  try {
    const credential = env.ACTUAL_SESSION_TOKEN
      ? { sessionToken: env.ACTUAL_SESSION_TOKEN }
      : { password: env.ACTUAL_PASSWORD };
    await api.init({ dataDir, serverURL: env.ACTUAL_SERVER_URL, ...credential });
  } catch (e) {
    throw classify(e);
  }
}

async function remoteBudgets() {
  const all = await api.getBudgets();
  // getBudgets() already drops deleted remote files (remoteFileModel.toExternal returns null
  // for them in @actual-app/api 26.11.0); the deleted check keeps that true if it changes.
  return all.filter((b) => b.state === 'remote' && b.groupId && !b.deleted);
}

async function check() {
  const syncId = env.ACTUAL_BUDGET_SYNC_ID;
  if (!syncId) throw new Exit(1, '', 'ACTUAL_BUDGET_SYNC_ID is not set');
  await open();
  try {
    const password = env.ACTUAL_BUDGET_ENCRYPTION_PASSWORD;
    await api.downloadBudget(syncId, password ? { password } : {});
  } catch (e) {
    throw classify(e);
  }
}

async function create(name) {
  if (!name) throw new Exit(1, '', 'usage: actual-admin.mjs create <name>');
  await open();
  const before = new Set((await remoteBudgets()).map((b) => b.groupId));
  await api.runImport(name, async () => {});
  const added = (await remoteBudgets()).filter((b) => !before.has(b.groupId));
  if (added.length !== 1) throw new Exit(1, '', `expected one new budget on the server, found ${added.length}`);
  out(added[0].groupId);
}

async function list() {
  await open();
  for (const b of await remoteBudgets()) out(`${b.groupId}\t${b.name}`);
}

const DEFAULT_TIMEOUT_S = 45;
const timeoutS = Number(env.ACTUAL_ADMIN_TIMEOUT_S) > 0 ? Number(env.ACTUAL_ADMIN_TIMEOUT_S) : DEFAULT_TIMEOUT_S;
// Covers the command and the shutdown after it; exits without waiting on either.
setTimeout(() => {
  out('timeout');
  err(`no answer from the server within ${timeoutS} s`);
  if (dataDir) rmSync(dataDir, { recursive: true, force: true });
  process.exit(4);
}, timeoutS * 1000);

const [cmd, ...args] = process.argv.slice(2);
const commands = { check, create: () => create(args[0]), list };

let code = 0;
try {
  if (!commands[cmd]) throw new Exit(1, '', 'usage: actual-admin.mjs check|create <name>|list');
  await commands[cmd]();
} catch (e) {
  if (e instanceof Exit) {
    if (e.word) out(e.word);
    if (e.message) err(e.message);
    code = e.exitCode;
  } else {
    err(e?.code ? `${e.code}: ${e.message}` : e?.message || e);
    code = 1;
  }
}
try {
  await api.shutdown();
} catch {
  // Nothing to close after a failed init.
}
if (dataDir) rmSync(dataDir, { recursive: true, force: true });
process.exit(code);
