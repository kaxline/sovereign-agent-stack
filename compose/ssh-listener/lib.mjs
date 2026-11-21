// Files the SSH listener keeps in its data folder (/data in the container):
//
//   keypair.json  {"publicKey":HEX,"secretKey":HEX}, mode 0600, made once
//   config.json   {"sshd":"HOST:PORT"}, the address streams are passed to
//   allowed.json  {"keys":[{"key":HEX,"name":TEXT|null,"added":ISO}]}
//   status.json   written by the running server: NAT state, open streams
//
// Every write goes to a temporary file first and is renamed into place, so a
// reader never sees half a file.
import fs from 'node:fs'
import path from 'node:path'
import net from 'node:net'
import HyperDHT from 'hyperdht'

export const KEY_RE = /^[0-9a-f]{64}$/

export function dataDir() {
  return process.env.SSH_LISTENER_DATA || '/data'
}

function file(dir, name) {
  return path.join(dir, name)
}

export function writeJson(target, value, mode = 0o644) {
  const tmp = `${target}.${process.pid}.tmp`
  fs.writeFileSync(tmp, JSON.stringify(value, null, 2) + '\n', { mode })
  fs.renameSync(tmp, target)
}

function readJson(target) {
  return JSON.parse(fs.readFileSync(target, 'utf8'))
}

// normalizeKey(text): the key in lower-case hex, or null when it isn't 32
// bytes written as 64 hex characters.
export function normalizeKey(text) {
  const key = String(text || '').trim().toLowerCase()
  return KEY_RE.test(key) ? key : null
}

// loadKeyPair(dir): the stored key pair as Buffers, or null before setup.
export function loadKeyPair(dir) {
  const target = file(dir, 'keypair.json')
  if (!fs.existsSync(target)) return null
  const { publicKey, secretKey } = readJson(target)
  return { publicKey: Buffer.from(publicKey, 'hex'), secretKey: Buffer.from(secretKey, 'hex') }
}

// ensureKeyPair(dir): the stored key pair, made the first time.
export function ensureKeyPair(dir) {
  const existing = loadKeyPair(dir)
  if (existing) return { keyPair: existing, created: false }
  const keyPair = HyperDHT.keyPair()
  writeJson(
    file(dir, 'keypair.json'),
    { publicKey: keyPair.publicKey.toString('hex'), secretKey: keyPair.secretKey.toString('hex') },
    0o600
  )
  return { keyPair, created: true }
}

export const DEFAULT_SSHD = '127.0.0.1:22'

export function loadConfig(dir) {
  const target = file(dir, 'config.json')
  const config = fs.existsSync(target) ? readJson(target) : {}
  return { sshd: config.sshd || DEFAULT_SSHD }
}

export function saveConfig(dir, config) {
  writeJson(file(dir, 'config.json'), config)
}

// parseAddress("HOST:PORT"): {host, port}, or null. IPv6 hosts go in brackets.
export function parseAddress(text) {
  const m = /^(?:\[([^\]]+)\]|([^:\s]+)):(\d{1,5})$/.exec(String(text || ''))
  if (!m) return null
  const port = Number(m[3])
  if (port < 1 || port > 65535) return null
  return { host: m[1] || m[2], port }
}

// loadAllowed(dir): the allowed list. A missing file is an empty list; a file
// that can't be read throws, and the server then refuses everyone.
export function loadAllowed(dir) {
  const target = file(dir, 'allowed.json')
  if (!fs.existsSync(target)) return []
  const { keys } = readJson(target)
  if (!Array.isArray(keys)) throw new Error(`${target} has no keys list`)
  return keys.filter((entry) => entry && normalizeKey(entry.key) === entry.key)
}

export function saveAllowed(dir, keys) {
  writeJson(file(dir, 'allowed.json'), { keys })
}

export function loadStatus(dir) {
  const target = file(dir, 'status.json')
  if (!fs.existsSync(target)) return null
  try {
    return readJson(target)
  } catch {
    return null
  }
}

export function saveStatus(dir, status) {
  writeJson(file(dir, 'status.json'), status)
}

export function clearStatus(dir) {
  fs.rmSync(file(dir, 'status.json'), { force: true })
}

// natType(dht): what HyperDHT has learned about this host's NAT. "open": peers
// reach it without hole punching. "consistent": firewalled, but the NAT keeps
// the same public port, so hole punching usually works. "randomized": the NAT
// picks a new port per peer, and a client behind a randomizing NAT too will
// fail to connect. "unknown": not learned yet.
export function natType(dht) {
  if (!dht.firewalled) return 'open'
  if (dht.randomized) return 'randomized'
  if (dht.host) return 'consistent'
  return 'unknown'
}

// reachable({host, port}, timeoutMs): whether a TCP connect succeeds.
export function reachable({ host, port }, timeoutMs = 2000) {
  return new Promise((resolve) => {
    const socket = net.connect({ host, port })
    const done = (ok) => {
      socket.destroy()
      resolve(ok)
    }
    socket.setTimeout(timeoutMs, () => done(false))
    socket.once('connect', () => done(true))
    socket.once('error', () => done(false))
  })
}
