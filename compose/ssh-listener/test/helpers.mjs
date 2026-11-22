// Shared by the listener's tests.
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

export function tmpDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'ssh-listener-'))
}

// connect(node, publicKey, keyPair): resolves with the open stream, rejects
// when the listener refuses.
export function connect(node, publicKey, keyPair) {
  return new Promise((resolve, reject) => {
    const stream = node.connect(publicKey, { keyPair })
    stream.once('open', () => resolve(stream))
    stream.once('error', reject)
  })
}

// closed(emitter, ms): resolves when it emits 'close', rejects after ms.
export function closed(emitter, ms) {
  return new Promise((resolve, reject) => {
    if (emitter.destroyed) return resolve()
    const timer = setTimeout(() => reject(new Error(`still open after ${ms} ms`)), ms)
    emitter.once('close', () => {
      clearTimeout(timer)
      resolve()
    })
    emitter.on('error', () => {})
  })
}
