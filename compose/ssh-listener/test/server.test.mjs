// The listener against a local HyperDHT testnet and a TCP server standing in
// for sshd. No internet needed.
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import net from 'node:net'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { fileURLToPath } from 'node:url'
import HyperDHT from 'hyperdht'
import createTestnet from 'hyperdht/testnet.js'
import { startServer } from '../server.mjs'
import { saveAllowed, saveConfig, loadStatus, loadKeyPair } from '../lib.mjs'

const run = promisify(execFile)
const LISTENER = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'listener.mjs')

// A fake sshd: echoes what it gets and counts connections.
async function fakeSshd() {
  const sockets = new Set()
  let accepted = 0
  const server = net.createServer((socket) => {
    accepted++
    sockets.add(socket)
    socket.on('close', () => sockets.delete(socket))
    socket.pipe(socket)
  })
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  return {
    address: `127.0.0.1:${server.address().port}`,
    accepted: () => accepted,
    close: () => {
      for (const s of sockets) s.destroy()
      return new Promise((resolve) => server.close(resolve))
    }
  }
}

function tmpDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'ssh-listener-'))
}

// connect(node, publicKey, keyPair): resolves with the open stream, rejects
// when the listener refuses.
function connect(node, publicKey, keyPair) {
  return new Promise((resolve, reject) => {
    const stream = node.connect(publicKey, { keyPair })
    stream.once('open', () => resolve(stream))
    stream.once('error', reject)
  })
}

function echo(stream, text) {
  return new Promise((resolve, reject) => {
    stream.once('data', (data) => resolve(data.toString()))
    stream.once('error', reject)
    stream.write(text)
  })
}

function closed(stream, ms) {
  return new Promise((resolve, reject) => {
    if (stream.destroyed) return resolve()
    const timer = setTimeout(() => reject(new Error(`stream still open after ${ms} ms`)), ms)
    stream.once('close', () => {
      clearTimeout(timer)
      resolve()
    })
    stream.on('error', () => {})
  })
}

async function setup(t) {
  const testnet = await createTestnet(3, t.after.bind(t))
  const sshd = await fakeSshd()
  t.after(() => sshd.close())
  const dir = tmpDir()
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }))
  saveConfig(dir, { sshd: sshd.address })
  const server = await startServer({ dir, dht: testnet.createNode(), pollMs: 50, log: (l) => t.diagnostic(l) })
  t.after(() => server.close())
  const client = testnet.createNode()
  t.after(() => client.destroy())
  const keyPair = HyperDHT.keyPair()
  return { testnet, sshd, dir, server, client, keyPair, hex: keyPair.publicKey.toString('hex') }
}

test('refuses a client not on the list before sshd sees it', async (t) => {
  const { sshd, server, client, keyPair } = await setup(t)
  await assert.rejects(connect(client, server.publicKey, keyPair))
  assert.equal(sshd.accepted(), 0)
})

test('allow takes effect without a restart, revoke closes the stream', async (t) => {
  const { sshd, dir, server, client, keyPair, hex } = await setup(t)
  saveAllowed(dir, [{ key: hex, name: 'laptop', added: new Date().toISOString() }])

  const stream = await connect(client, server.publicKey, keyPair)
  assert.equal(await echo(stream, 'SSH-2.0-test\r\n'), 'SSH-2.0-test\r\n')
  assert.equal(sshd.accepted(), 1)
  assert.equal(server.openCount(), 1)
  assert.equal(loadStatus(dir).connections, 1)

  saveAllowed(dir, [])
  await closed(stream, 3000)
  await assert.rejects(connect(client, server.publicKey, keyPair))
  assert.equal(sshd.accepted(), 1)
})

test('another allowed client keeps its stream when one key is revoked', async (t) => {
  const { dir, server, client, keyPair, hex } = await setup(t)
  const other = HyperDHT.keyPair()
  const otherHex = other.publicKey.toString('hex')
  const added = new Date().toISOString()
  saveAllowed(dir, [
    { key: hex, name: null, added },
    { key: otherHex, name: null, added }
  ])
  const first = await connect(client, server.publicKey, keyPair)
  const second = await connect(client, server.publicKey, other)
  saveAllowed(dir, [{ key: otherHex, name: null, added }])
  await closed(first, 3000)
  assert.equal(await echo(second, 'still here'), 'still here')
})

test('an unreadable allowed list refuses everyone', async (t) => {
  const { dir, server, client, keyPair } = await setup(t)
  fs.writeFileSync(path.join(dir, 'allowed.json'), '{not json')
  await assert.rejects(connect(client, server.publicKey, keyPair))
})

test('the key pair stays the same across restarts', async (t) => {
  const testnet = await createTestnet(3, t.after.bind(t))
  const dir = tmpDir()
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }))
  const first = await startServer({ dir, dht: testnet.createNode() })
  const key = first.publicKey.toString('hex')
  assert.ok(loadStatus(dir), 'status.json written while running')
  await first.close()
  assert.equal(loadStatus(dir), null, 'status.json removed on close')
  const second = await startServer({ dir, dht: testnet.createNode() })
  t.after(() => second.close())
  assert.equal(second.publicKey.toString('hex'), key)
  assert.equal(fs.statSync(path.join(dir, 'keypair.json')).mode & 0o777, 0o600)
})

// --- the command line -------------------------------------------------------

async function cli(dir, ...args) {
  try {
    const { stdout } = await run('node', [LISTENER, ...args, '--json'], {
      env: { ...process.env, SSH_LISTENER_DATA: dir, SSH_LISTENER_VERSION: 'v9.9.9' }
    })
    return { code: 0, out: JSON.parse(stdout) }
  } catch (err) {
    return { code: err.code, out: JSON.parse(err.stdout) }
  }
}

test('init keeps the key pair and a stored sshd address', async (t) => {
  const dir = tmpDir()
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }))
  const first = await cli(dir, 'init', '--default-sshd', 'host.docker.internal:22')
  assert.equal(first.out.created, true)
  assert.equal(first.out.sshd, 'host.docker.internal:22')
  const again = await cli(dir, 'init', '--default-sshd', '127.0.0.1:22')
  assert.equal(again.out.created, false)
  assert.equal(again.out.publicKey, first.out.publicKey)
  assert.equal(again.out.sshd, 'host.docker.internal:22')
  const moved = await cli(dir, 'init', '--sshd', '10.0.0.5:2222')
  assert.equal(moved.out.sshd, '10.0.0.5:2222')
  assert.equal((await cli(dir, 'init', '--sshd', 'nope')).code, 2)
})

test('allow, list and revoke', async (t) => {
  const dir = tmpDir()
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }))
  const key = 'AB'.repeat(32)
  const allowed = await cli(dir, 'allow', key, '--name', 'laptop')
  assert.equal(allowed.code, 0)
  assert.deepEqual(
    { key: allowed.out.key, name: allowed.out.name, alreadyAllowed: allowed.out.alreadyAllowed },
    { key: 'ab'.repeat(32), name: 'laptop', alreadyAllowed: false }
  )
  const again = await cli(dir, 'allow', key)
  assert.equal(again.out.alreadyAllowed, true)
  assert.equal(again.out.name, 'laptop')
  assert.equal(again.out.added, allowed.out.added)

  const listed = await cli(dir, 'list')
  assert.equal(listed.out.keys.length, 1)

  assert.deepEqual((await cli(dir, 'revoke', key)).out, { key: 'ab'.repeat(32), revoked: true })
  assert.deepEqual((await cli(dir, 'revoke', key)).out, { key: 'ab'.repeat(32), revoked: false })
  assert.equal((await cli(dir, 'list')).out.keys.length, 0)

  const bad = await cli(dir, 'allow', 'abc')
  assert.equal(bad.code, 2)
  assert.equal(bad.out.error, 'bad_input')
})

test('status while stopped, before and after setup', async (t) => {
  const dir = tmpDir()
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }))
  const before = await cli(dir, 'status', '--running', 'false')
  assert.equal(before.code, 0)
  assert.equal(before.out.running, false)
  assert.equal(before.out.setUp, false)
  assert.equal(before.out.publicKey, null)

  const sshd = await fakeSshd()
  t.after(() => sshd.close())
  await cli(dir, 'init', '--sshd', sshd.address)
  const up = await cli(dir, 'status', '--running', 'false', '--warning', 'check this')
  assert.equal(up.out.setUp, true)
  assert.equal(up.out.publicKey, loadKeyPair(dir).publicKey.toString('hex'))
  assert.equal(up.out.sshdReachable, true)
  assert.equal(up.out.allowed, 0)
  assert.equal(up.out.version, 'v9.9.9')
  assert.equal(up.out.natType, null)
  assert.deepEqual(up.out.warnings, ['check this'])

  await cli(dir, 'init', '--sshd', '127.0.0.1:1')
  const down = await cli(dir, 'status', '--running', 'false', '--unreachable-hint', 'turn on sshd')
  assert.equal(down.out.sshdReachable, false)
  assert.deepEqual(down.out.warnings, ['turn on sshd'])
})
