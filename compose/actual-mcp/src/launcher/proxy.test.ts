import http from 'node:http';
import type { AddressInfo } from 'node:net';
import { afterEach, describe, expect, it } from 'vitest';
import { createProxy } from './proxy.js';

const servers: http.Server[] = [];

async function listen(server: http.Server): Promise<number> {
  servers.push(server);
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  return (server.address() as AddressInfo).port;
}

/** A child that answers with its name and the path it saw. */
function echoChild(name: string): http.Server {
  return http.createServer((req, res) => {
    let body = '';
    req.on('data', (chunk) => (body += chunk));
    req.on('end', () => {
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ name, url: req.url, method: req.method, body }));
    });
  });
}

afterEach(async () => {
  for (const server of servers.splice(0)) {
    server.closeAllConnections();
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
});

describe('createProxy', () => {
  it('routes /<slug>/mcp to that child and /mcp, /sse, /messages to the legacy child', async () => {
    const aPort = await listen(echoChild('a'));
    const legacyPort = await listen(echoChild('legacy'));
    const port = await listen(createProxy((slug) => (slug === 'a' ? aPort : undefined), legacyPort));
    const base = `http://127.0.0.1:${port}`;

    const a = await fetch(`${base}/a/mcp?x=1`, { method: 'POST', body: '{"hi":1}' });
    expect(await a.json()).toEqual({ name: 'a', url: '/mcp?x=1', method: 'POST', body: '{"hi":1}' });

    for (const p of ['/mcp', '/sse', '/messages?connectionId=abc']) {
      const res = await fetch(`${base}${p}`);
      expect(await res.json()).toMatchObject({ name: 'legacy', url: p });
    }

    expect((await fetch(`${base}/nope/mcp`)).status).toBe(404);
    expect((await fetch(`${base}/a/other`)).status).toBe(404);
    expect((await fetch(`${base}/`)).status).toBe(404);
  });

  it('answers 404 on legacy paths when there is no legacy child', async () => {
    const port = await listen(createProxy(() => undefined, undefined));

    expect((await fetch(`http://127.0.0.1:${port}/mcp`)).status).toBe(404);
  });

  it('answers 502 when the child is not listening', async () => {
    const dead = await listen(echoChild('dead'));
    const deadServer = servers.pop() as http.Server;
    await new Promise<void>((resolve) => deadServer.close(() => resolve()));
    const port = await listen(createProxy(() => dead, undefined));

    expect((await fetch(`http://127.0.0.1:${port}/a/mcp`)).status).toBe(502);
  });

  it('turns an unknown-session 400 into a 404 with the same body, and leaves other 400s alone', async () => {
    const body = JSON.stringify({
      jsonrpc: '2.0',
      error: { code: -32000, message: 'Bad Request: No valid session ID provided' },
      id: null,
    });
    const child = http.createServer((req, res) => {
      res.writeHead(400, { 'content-type': 'application/json' });
      res.end(req.url === '/mcp' ? body : '{"error":"Invalid or missing connectionId"}');
    });
    const childPort = await listen(child);
    const port = await listen(createProxy(() => childPort, childPort));

    const rewritten = await fetch(`http://127.0.0.1:${port}/a/mcp`, { method: 'POST' });
    expect(rewritten.status).toBe(404);
    expect(await rewritten.text()).toBe(body);

    const legacy = await fetch(`http://127.0.0.1:${port}/mcp`, { method: 'POST' });
    expect(legacy.status).toBe(404);

    const other = await fetch(`http://127.0.0.1:${port}/messages`, { method: 'POST' });
    expect(other.status).toBe(400);
    expect(await other.text()).toBe('{"error":"Invalid or missing connectionId"}');
  });

  it('passes a streaming response through as it arrives', async () => {
    let finish: () => void = () => {};
    const child = http.createServer((_req, res) => {
      res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' });
      res.write('data: one\n\n');
      finish = () => res.end('data: two\n\n');
    });
    const childPort = await listen(child);
    const port = await listen(createProxy(() => childPort, undefined));

    const res = await fetch(`http://127.0.0.1:${port}/a/mcp`, { headers: { accept: 'text/event-stream' } });
    expect(res.status).toBe(200);
    expect(res.headers.get('content-type')).toBe('text/event-stream');
    const reader = (res.body as ReadableStream<Uint8Array>).getReader();
    const decoder = new TextDecoder();

    const first = await reader.read();
    expect(decoder.decode(first.value)).toBe('data: one\n\n');

    finish();
    let rest = '';
    for (let chunk = await reader.read(); !chunk.done; chunk = await reader.read()) {
      rest += decoder.decode(chunk.value);
    }
    expect(rest).toBe('data: two\n\n');
  });

  it('ends the client stream when the child dies mid-stream', async () => {
    let die: () => void = () => {};
    const child = http.createServer((_req, res) => {
      res.writeHead(200, { 'content-type': 'text/event-stream' });
      res.write('data: one\n\n');
      die = () => res.socket?.destroy();
    });
    const childPort = await listen(child);
    const proxy = createProxy(() => childPort, undefined);
    const port = await listen(proxy);

    const res = await fetch(`http://127.0.0.1:${port}/a/mcp`, { headers: { accept: 'text/event-stream' } });
    const reader = (res.body as ReadableStream<Uint8Array>).getReader();
    await reader.read();

    die();
    const outcome = await Promise.race([
      (async () => {
        try {
          for (let chunk = await reader.read(); !chunk.done; chunk = await reader.read()) {
            // drain
          }
          return 'ended';
        } catch {
          return 'errored';
        }
      })(),
      new Promise<string>((resolve) => setTimeout(() => resolve('hung'), 2_000)),
    ]);
    expect(outcome).not.toBe('hung');
  });

  it('closes the child side when the client hangs up', async () => {
    let childClosed: () => void = () => {};
    const closed = new Promise<void>((resolve) => (childClosed = resolve));
    const child = http.createServer((req, res) => {
      res.writeHead(200, { 'content-type': 'text/event-stream' });
      res.write('data: one\n\n');
      res.on('close', () => childClosed());
    });
    const childPort = await listen(child);
    const port = await listen(createProxy(() => childPort, undefined));

    const controller = new AbortController();
    const res = await fetch(`http://127.0.0.1:${port}/a/mcp`, { signal: controller.signal });
    await (res.body as ReadableStream<Uint8Array>).getReader().read();
    controller.abort();

    await expect(
      Promise.race([closed.then(() => 'closed'), new Promise((r) => setTimeout(() => r('open'), 2_000))])
    ).resolves.toBe('closed');
  });
});
