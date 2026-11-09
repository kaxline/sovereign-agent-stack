/**
 * The launcher's HTTP front: `/<slug>/mcp` goes to that row's child, `/mcp`, `/sse` and
 * `/messages` go to the legacy child.
 */
import http from 'node:http';

const LEGACY_PATHS = new Set(['/mcp', '/sse', '/messages']);
const ROW_PATH_RE = /^\/([^/]+)\/mcp$/;
const UNKNOWN_SESSION = 'No valid session ID provided';
// Reason: hop-by-hop headers describe one connection and must not be forwarded.
const HOP_BY_HOP = ['connection', 'keep-alive', 'proxy-connection', 'transfer-encoding', 'upgrade'];

function withoutHopByHop(headers: http.IncomingHttpHeaders): http.IncomingHttpHeaders {
  const copy = { ...headers };
  for (const name of HOP_BY_HOP) delete copy[name];
  return copy;
}

function sendText(res: http.ServerResponse, status: number, message: string): void {
  res.writeHead(status, { 'content-type': 'application/json' });
  res.end(JSON.stringify({ error: message }));
}

/** Resolves a request path to the child port and the path to ask it for. */
function route(
  pathname: string,
  lookup: (slug: string) => number | undefined,
  legacyPort: number | undefined
): { port: number; path: string } | undefined {
  if (LEGACY_PATHS.has(pathname)) {
    return legacyPort === undefined ? undefined : { port: legacyPort, path: pathname };
  }
  const match = ROW_PATH_RE.exec(pathname);
  if (!match) return undefined;
  const port = lookup(match[1]);
  return port === undefined ? undefined : { port, path: '/mcp' };
}

export function createProxy(lookup: (slug: string) => number | undefined, legacyPort: number | undefined): http.Server {
  return http.createServer((req, res) => {
    const url = new URL(req.url ?? '/', 'http://launcher');
    const target = route(url.pathname, lookup, legacyPort);
    if (!target) {
      sendText(res, 404, 'Not found');
      return;
    }

    const upstream = http.request(
      {
        host: '127.0.0.1',
        port: target.port,
        method: req.method,
        path: `${target.path}${url.search}`,
        headers: withoutHopByHop(req.headers),
      },
      (childRes) => {
        const headers = withoutHopByHop(childRes.headers);
        const status = childRes.statusCode ?? 502;
        if (status !== 400) {
          res.writeHead(status, headers);
          // Reason: flush headers now so an SSE client sees the stream open before the first event.
          res.flushHeaders();
          childRes.pipe(res);
          return;
        }
        // Reason: the server answers an unknown MCP session id with 400; Hermes's client only
        // starts a new session on 404. These bodies are small, so buffer them to look.
        const chunks: Buffer[] = [];
        childRes.on('data', (chunk: Buffer) => chunks.push(chunk));
        childRes.on('end', () => {
          const body = Buffer.concat(chunks);
          res.writeHead(body.includes(UNKNOWN_SESSION) ? 404 : 400, headers);
          res.end(body);
        });
        childRes.on('error', () => res.destroy());
      }
    );

    upstream.on('error', () => {
      if (!res.headersSent) {
        sendText(res, 502, 'MCP server unavailable');
      } else {
        res.destroy();
      }
    });
    // Reason: a client that hangs up on a stream should close the child's side too.
    res.on('close', () => {
      if (!res.writableFinished) upstream.destroy();
    });
    req.pipe(upstream);
  });
}
