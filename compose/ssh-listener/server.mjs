// The listener: accepts HyperDHT streams on the stored key pair from clients
// on the allowed list and passes each one to sshd over TCP.
//
// The firewall reads allowed.json on every connection attempt, so `allow`
// takes effect at once. When the file changes (checked every
// SSH_LISTENER_POLL_MS, 1000 by default) or the process gets SIGHUP, open
// streams from keys no longer on the list are closed. A refused client never
// gets a stream, so sshd never sees it.
import fs from 'node:fs'
import path from 'node:path'
import net from 'node:net'
import HyperDHT from 'hyperdht'
import {
  ensureKeyPair,
  loadAllowed,
  loadConfig,
  parseAddress,
  saveStatus,
  clearStatus,
  natType
} from './lib.mjs'

// dht: a HyperDHT node to use instead of a new one on the public network
// (tests pass one from a local testnet); close() destroys it either way.
export async function startServer({ dir, dht = null, pollMs = 1000, log = () => {} }) {
  const { keyPair } = ensureKeyPair(dir)
  const { sshd } = loadConfig(dir)
  const target = parseAddress(sshd)
  if (!target) throw new Error(`sshd address ${sshd} is not HOST:PORT`)

  const streams = new Map() // client key hex -> Set of open streams
  const startedAt = new Date().toISOString()
  dht = dht || new HyperDHT()

  const allowedKeys = () => {
    try {
      return new Set(loadAllowed(dir).map((entry) => entry.key))
    } catch (err) {
      log(`allowed list unreadable, refusing everyone: ${err.message}`)
      return new Set()
    }
  }

  const openCount = () => {
    let n = 0
    for (const set of streams.values()) n += set.size
    return n
  }

  const writeStatus = () => {
    try {
      saveStatus(dir, {
        pid: process.pid,
        startedAt,
        updated: new Date().toISOString(),
        natType: natType(dht),
        firewalled: dht.firewalled,
        randomized: dht.randomized,
        connections: openCount()
      })
    } catch (err) {
      log(`could not write status: ${err.message}`)
    }
  }

  const closeRevoked = () => {
    const allowed = allowedKeys()
    for (const [key, set] of streams) {
      if (allowed.has(key)) continue
      log(`closing ${set.size} stream(s) from revoked key ${key}`)
      for (const stream of set) stream.destroy()
    }
  }

  const onConnection = (stream) => {
    const key = stream.remotePublicKey.toString('hex')
    // The list may have changed between the firewall check and now.
    if (!allowedKeys().has(key)) {
      stream.destroy()
      return
    }
    if (!streams.has(key)) streams.set(key, new Set())
    streams.get(key).add(stream)

    const tcp = net.connect({ host: target.host, port: target.port, allowHalfOpen: true })
    const done = () => {
      stream.destroy()
      tcp.destroy()
      const set = streams.get(key)
      if (set && set.delete(stream) && set.size === 0) streams.delete(key)
      writeStatus()
    }
    stream.on('error', (err) => log(`stream from ${key}: ${err.message}`))
    tcp.on('error', (err) => log(`sshd ${sshd}: ${err.message}`))
    stream.on('close', done)
    tcp.on('close', done)
    stream.pipe(tcp).pipe(stream)
    log(`stream from ${key}`)
    writeStatus()
  }

  const server = dht.createServer(
    {
      // true refuses the client.
      firewall: (remotePublicKey) => !allowedKeys().has(remotePublicKey.toString('hex'))
    },
    onConnection
  )
  await server.listen(keyPair)

  dht.on('nat-update', writeStatus)
  writeStatus()

  const allowedFile = path.join(dir, 'allowed.json')
  fs.watchFile(allowedFile, { interval: pollMs }, closeRevoked)
  const onHup = () => closeRevoked()
  process.on('SIGHUP', onHup)

  return {
    publicKey: keyPair.publicKey,
    dht,
    openCount,
    async close() {
      fs.unwatchFile(allowedFile, closeRevoked)
      process.off('SIGHUP', onHup)
      for (const set of streams.values()) for (const stream of set) stream.destroy()
      await server.close()
      await dht.destroy()
      clearStatus(dir)
    }
  }
}
