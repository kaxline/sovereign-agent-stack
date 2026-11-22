// The listener in front of a real OpenSSH sshd, with an ssh2 client over the
// HyperDHT stream, the way Boundary connects: the host key check, sign-in, a
// command, SFTP and a port forward. Runs in the image's test stage, which has
// sshd and ssh2; elsewhere it skips.
import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import net from 'node:net'
import crypto from 'node:crypto'
import { spawn, execFileSync } from 'node:child_process'
import HyperDHT from 'hyperdht'
import createTestnet from 'hyperdht/testnet.js'
import { startServer } from '../server.mjs'
import { saveAllowed, saveConfig, reachable } from '../lib.mjs'
import { tmpDir, connect, closed } from './helpers.mjs'

const SSHD = '/usr/sbin/sshd'
let Client = null
try {
  ;({ Client } = await import('ssh2'))
} catch {}
const skip = !fs.existsSync(SSHD) || !Client ? 'needs sshd and ssh2 (the image test stage has both)' : false

function freePort() {
  return new Promise((resolve) => {
    const server = net.createServer()
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address()
      server.close(() => resolve(port))
    })
  })
}

function keygen(file) {
  execFileSync('ssh-keygen', ['-q', '-t', 'ed25519', '-N', '', '-f', file])
}

// fingerprint(pubFile): the SHA-256 fingerprint, as ssh-keygen -l prints it
// and as Boundary stores it.
function fingerprint(pubFile) {
  return execFileSync('ssh-keygen', ['-l', '-E', 'sha256', '-f', pubFile]).toString().split(' ')[1]
}

// sshd(t, dir): a real sshd on 127.0.0.1, signing in the current user by the
// key in dir/user_ed25519.
async function sshd(t, dir) {
  keygen(path.join(dir, 'host_ed25519'))
  keygen(path.join(dir, 'user_ed25519'))
  fs.copyFileSync(path.join(dir, 'user_ed25519.pub'), path.join(dir, 'authorized_keys'))
  const port = await freePort()
  const config = path.join(dir, 'sshd_config')
  fs.writeFileSync(
    config,
    [
      `Port ${port}`,
      'ListenAddress 127.0.0.1',
      `HostKey ${dir}/host_ed25519`,
      `AuthorizedKeysFile ${dir}/authorized_keys`,
      `PidFile ${dir}/sshd.pid`,
      'PasswordAuthentication no',
      'KbdInteractiveAuthentication no',
      'PermitRootLogin prohibit-password',
      'UsePAM no',
      // os.tmpdir() is world-writable, which StrictModes rejects.
      'StrictModes no',
      'AllowTcpForwarding yes',
      'Subsystem sftp internal-sftp'
    ].join('\n') + '\n'
  )
  const proc = spawn(SSHD, ['-D', '-e', '-f', config], { stdio: ['ignore', 'ignore', 'pipe'] })
  let log = ''
  proc.stderr.on('data', (d) => (log += d))
  t.after(() => proc.kill())
  const address = { host: '127.0.0.1', port }
  for (let i = 0; i < 50 && !(await reachable(address, 200)); i++) {
    if (proc.exitCode !== null) throw new Error(`sshd exited: ${log}`)
    await new Promise((resolve) => setTimeout(resolve, 100))
  }
  return { address: `127.0.0.1:${port}`, hostFingerprint: fingerprint(path.join(dir, 'host_ed25519.pub')) }
}

// sshConnect(stream, opts): an ssh2 client signed in over the stream.
// hostKeys collects the fingerprints the client was shown.
function sshConnect(stream, { privateKey, expectedFingerprint, hostKeys }) {
  return new Promise((resolve, reject) => {
    const client = new Client()
    client.once('ready', () => resolve(client))
    client.once('error', reject)
    client.connect({
      sock: stream,
      username: os.userInfo().username,
      privateKey,
      hostVerifier: (key) => {
        const seen = 'SHA256:' + crypto.createHash('sha256').update(key).digest('base64').replace(/=+$/, '')
        hostKeys.push(seen)
        return seen === expectedFingerprint
      }
    })
  })
}

function execOut(client, command) {
  return new Promise((resolve, reject) => {
    client.exec(command, (err, channel) => {
      if (err) return reject(err)
      let out = ''
      channel.on('data', (d) => (out += d))
      channel.stderr.on('data', () => {})
      channel.on('close', (code) => (code === 0 ? resolve(out) : reject(new Error(`${command} exited ${code}`))))
    })
  })
}

async function setup(t) {
  const testnet = await createTestnet(3, t.after.bind(t))
  const dir = tmpDir()
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }))
  const real = await sshd(t, dir)
  saveConfig(dir, { sshd: real.address })
  const server = await startServer({ dir, dht: testnet.createNode(), pollMs: 50, log: (l) => t.diagnostic(l) })
  t.after(() => server.close())
  const node = testnet.createNode()
  t.after(() => node.destroy())
  const keyPair = HyperDHT.keyPair()
  saveAllowed(dir, [{ key: keyPair.publicKey.toString('hex'), name: 'test', added: new Date().toISOString() }])
  return { dir, server, node, keyPair, real, privateKey: fs.readFileSync(path.join(dir, 'user_ed25519')) }
}

test('an allowed client signs in to sshd: host key, a command, SFTP and a port forward', { skip, timeout: 30000 }, async (t) => {
  const { dir, server, node, keyPair, real, privateKey } = await setup(t)
  const hostKeys = []
  const stream = await connect(node, server.publicKey, keyPair)
  const client = await sshConnect(stream, { privateKey, expectedFingerprint: real.hostFingerprint, hostKeys })
  t.after(() => client.end())
  assert.deepEqual(hostKeys, [real.hostFingerprint])

  assert.equal(await execOut(client, 'echo through-the-listener'), 'through-the-listener\n')

  const sftp = await new Promise((resolve, reject) => client.sftp((err, s) => (err ? reject(err) : resolve(s))))
  const remote = path.join(dir, 'sftp.txt')
  await new Promise((resolve, reject) => sftp.writeFile(remote, 'over sftp', (err) => (err ? reject(err) : resolve())))
  assert.equal(fs.readFileSync(remote, 'utf8'), 'over sftp')

  // A port forward to a service on the host's loopback, as Boundary reaches
  // the stack's published ports.
  const service = net.createServer((socket) => socket.end('from the host'))
  await new Promise((resolve) => service.listen(0, '127.0.0.1', resolve))
  t.after(() => service.close())
  const forwarded = await new Promise((resolve, reject) => {
    client.forwardOut('127.0.0.1', 0, '127.0.0.1', service.address().port, (err, channel) => {
      if (err) return reject(err)
      let got = ''
      channel.on('data', (d) => (got += d))
      // The service ended its side; the channel closes once ours ends too.
      channel.on('end', () => {
        channel.end()
        resolve(got)
      })
    })
  })
  assert.equal(forwarded, 'from the host')
})

test('sshd still refuses an SSH key it does not know, though the listener lets the stream through', { skip, timeout: 30000 }, async (t) => {
  const { dir, server, node, keyPair, real } = await setup(t)
  keygen(path.join(dir, 'stranger_ed25519'))
  const stream = await connect(node, server.publicKey, keyPair)
  await assert.rejects(
    sshConnect(stream, {
      privateKey: fs.readFileSync(path.join(dir, 'stranger_ed25519')),
      expectedFingerprint: real.hostFingerprint,
      hostKeys: []
    }),
    (err) => err.level === 'client-authentication'
  )
})

test('revoke ends a signed-in SSH session', { skip, timeout: 30000 }, async (t) => {
  const { dir, server, node, keyPair, real, privateKey } = await setup(t)
  const stream = await connect(node, server.publicKey, keyPair)
  const client = await sshConnect(stream, { privateKey, expectedFingerprint: real.hostFingerprint, hostKeys: [] })
  client.on('error', () => {})
  assert.equal(await execOut(client, 'echo before'), 'before\n')
  saveAllowed(dir, [])
  await closed(client, 2000)
})
