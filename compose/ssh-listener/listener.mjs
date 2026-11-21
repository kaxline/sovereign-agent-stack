#!/usr/bin/env node
// Entry point of the SSH listener image. scripts/ssh-listener.sh runs these
// commands with `docker exec` while the listener runs and `docker run --rm`
// while it is stopped, against the same data volume.
//
//   serve                                  run the listener
//   init [--sshd HOST:PORT] [--default-sshd HOST:PORT]
//                                          make the key pair if missing; store
//                                          --sshd, or --default-sshd when no
//                                          address is stored yet
//   allow KEY [--name LABEL]               add a client key (64 hex characters)
//   revoke KEY                             remove one; the server closes its streams
//   list                                   the allowed keys
//   status [--running true|false] [--warning TEXT]... [--unreachable-hint TEXT]
//                                          the hint joins the warnings when sshd
//                                          doesn't answer
//
// --json prints one JSON object. Errors print {"error":REASON,"message":TEXT}
// with --json, else the message on stderr; bad_input exits 2.
import {
  dataDir,
  ensureKeyPair,
  loadKeyPair,
  loadConfig,
  saveConfig,
  parseAddress,
  normalizeKey,
  loadAllowed,
  saveAllowed,
  loadStatus,
  reachable
} from './lib.mjs'
import { startServer } from './server.mjs'
import fs from 'node:fs'
import HyperDHT from 'hyperdht'
import path from 'node:path'

const VERSION = process.env.SSH_LISTENER_VERSION || 'dev'

class UsageError extends Error {}

function parseArgs(argv) {
  const opts = { json: false, positional: [], warning: [] }
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i]
    if (arg === '--json') opts.json = true
    else if (arg === '--warning') opts.warning.push(argv[++i] ?? '')
    else if (arg.startsWith('--')) {
      const value = argv[i + 1]
      if (value === undefined) throw new UsageError(`${arg} needs a value`)
      opts[arg.slice(2)] = value
      i++
    } else opts.positional.push(arg)
  }
  return opts
}

function keyArg(opts) {
  const key = normalizeKey(opts.positional[0])
  if (!key) throw new UsageError('a client key is 32 bytes written as 64 hex characters')
  return key
}

async function status(dir, opts) {
  const running = opts.running === 'true'
  const keyPair = loadKeyPair(dir)
  const { sshd } = loadConfig(dir)
  const address = parseAddress(sshd)
  let allowed = null
  try {
    allowed = loadAllowed(dir).length
  } catch {
    opts.warning.push('allowed.json cannot be read, so every client is refused')
  }
  const live = running ? loadStatus(dir) : null
  const sshdReachable = address ? await reachable(address) : false
  if (!sshdReachable && opts['unreachable-hint']) opts.warning.push(opts['unreachable-hint'])
  return {
    running,
    setUp: keyPair !== null,
    publicKey: keyPair ? keyPair.publicKey.toString('hex') : null,
    sshd,
    sshdReachable,
    allowed,
    version: VERSION,
    natType: live ? live.natType : null,
    connections: live ? live.connections : 0,
    warnings: opts.warning
  }
}

function print(opts, value, text) {
  if (opts.json) process.stdout.write(JSON.stringify(value) + '\n')
  else process.stdout.write(text + '\n')
}

async function serve(dir) {
  const log = (line) => process.stdout.write(`${new Date().toISOString()} ${line}\n`)
  const bootstrap = process.env.SSH_LISTENER_BOOTSTRAP
    ? process.env.SSH_LISTENER_BOOTSTRAP.split(',').map((entry) => {
        const address = parseAddress(entry)
        if (!address) throw new UsageError(`SSH_LISTENER_BOOTSTRAP entry ${entry} is not HOST:PORT`)
        return address
      })
    : undefined
  const pollMs = Number(process.env.SSH_LISTENER_POLL_MS) || 1000
  const dht = bootstrap ? new HyperDHT({ bootstrap }) : null
  const server = await startServer({ dir, dht, pollMs, log })
  log(`listening on ${server.publicKey.toString('hex')}, passing streams to ${loadConfig(dir).sshd}`)
  let closing = false
  const stop = async () => {
    if (closing) return
    closing = true
    await server.close()
    process.exit(0)
  }
  process.on('SIGTERM', stop)
  process.on('SIGINT', stop)
}

async function main(argv) {
  const [command, ...rest] = argv
  const opts = parseArgs(rest)
  const dir = dataDir()
  fs.mkdirSync(dir, { recursive: true })

  switch (command) {
    case 'serve':
      return serve(dir)

    case 'init': {
      for (const flag of ['sshd', 'default-sshd']) {
        if (opts[flag] !== undefined && !parseAddress(opts[flag])) {
          throw new UsageError(`--${flag} ${opts[flag]} is not HOST:PORT`)
        }
      }
      const { keyPair, created } = ensureKeyPair(dir)
      const stored = fs.existsSync(path.join(dir, 'config.json'))
      if (opts.sshd) saveConfig(dir, { sshd: opts.sshd })
      else if (!stored && opts['default-sshd']) saveConfig(dir, { sshd: opts['default-sshd'] })
      const publicKey = keyPair.publicKey.toString('hex')
      const { sshd } = loadConfig(dir)
      return print(opts, { publicKey, created, sshd }, `${created ? 'made' : 'kept'} key pair ${publicKey}`)
    }

    case 'allow': {
      const key = keyArg(opts)
      const keys = loadAllowed(dir)
      const existing = keys.find((entry) => entry.key === key)
      const name = opts.name === undefined ? (existing?.name ?? null) : opts.name
      const entry = { key, name, added: existing?.added ?? new Date().toISOString() }
      saveAllowed(dir, existing ? keys.map((e) => (e.key === key ? entry : e)) : [...keys, entry])
      return print(
        opts,
        { ...entry, alreadyAllowed: Boolean(existing) },
        `${existing ? 'already allowed' : 'allowed'} ${key}${name ? ` (${name})` : ''}`
      )
    }

    case 'revoke': {
      const key = keyArg(opts)
      const keys = loadAllowed(dir)
      const revoked = keys.some((entry) => entry.key === key)
      if (revoked) saveAllowed(dir, keys.filter((entry) => entry.key !== key))
      return print(opts, { key, revoked }, revoked ? `revoked ${key}` : `${key} was not allowed`)
    }

    case 'list': {
      const keys = loadAllowed(dir)
      const text = keys.length
        ? keys.map((e) => `${e.key}  ${e.added}  ${e.name ?? ''}`.trimEnd()).join('\n')
        : 'no allowed keys'
      return print(opts, { keys }, text)
    }

    case 'status': {
      const value = await status(dir, opts)
      const lines = [
        `running: ${value.running}`,
        `public key: ${value.publicKey ?? '(not set up)'}`,
        `sshd: ${value.sshd} (${value.sshdReachable ? 'reachable' : 'not reachable'})`,
        `allowed keys: ${value.allowed ?? 'unreadable'}`,
        `version: ${value.version}`
      ]
      if (value.natType) lines.push(`NAT: ${value.natType}`)
      for (const warning of value.warnings) lines.push(`warning: ${warning}`)
      return print(opts, value, lines.join('\n'))
    }

    default:
      throw new UsageError(`unknown command ${command ?? '(none)'}`)
  }
}

const json = process.argv.includes('--json')
main(process.argv.slice(2)).catch((err) => {
  const reason = err instanceof UsageError ? 'bad_input' : 'command_failed'
  if (json) process.stdout.write(JSON.stringify({ error: reason, message: err.message }) + '\n')
  else process.stderr.write(`error: ${err.message}\n`)
  process.exit(reason === 'bad_input' ? 2 : 4)
})
