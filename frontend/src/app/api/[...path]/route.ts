import { NextRequest, NextResponse } from 'next/server';

// A bounded same-origin proxy. The backend enforces its own configurable, lower limit.
const MAX_BYTES = 10 * 1024 * 1024;
async function proxy(req: NextRequest, { params }: { params: Promise<{ path: string[] }> }) {
  const { path } = await params;
  const port = process.env.RELAYOPS_API_PORT || '8000';
  if (!/^\d{1,5}$/.test(port) || Number(port) < 1 || Number(port) > 65535) {
    return NextResponse.json({ detail: 'Invalid API port configuration' }, { status: 503 });
  }
  const target = `http://127.0.0.1:${port}/api/` + path.map(encodeURIComponent).join('/') + req.nextUrl.search;
  const headers = new Headers();
  for (const name of ['content-type', 'content-encoding', 'cookie', 'origin', 'sec-fetch-site', 'x-webhook-secret', 'idempotency-key', 'x-event-id']) {
    const value = req.headers.get(name);
    if (value !== null) headers.set(name, value);
  }
  let body: ArrayBuffer | undefined;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 30_000);
  const reader = req.body?.getReader();
  const abortRead = () => { void reader?.cancel(); };
  controller.signal.addEventListener('abort', abortRead);
  try {
    if (Number(req.headers.get('content-length')) > MAX_BYTES) {
      await reader?.cancel();
      return NextResponse.json({ detail: 'Request body is too large' }, { status: 413 });
    }
    if (reader && req.method !== 'GET' && req.method !== 'HEAD') {
      const chunks: Uint8Array[] = [];
      let size = 0;
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > MAX_BYTES) {
          await reader.cancel();
          return NextResponse.json({ detail: 'Request body is too large' }, { status: 413 });
        }
        chunks.push(value);
      }
      const bytes = new Uint8Array(size);
      let offset = 0;
      for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
      body = bytes.buffer;
    }
    const res = await fetch(target, { method: req.method, headers, body, cache: 'no-store', redirect: 'manual', signal: controller.signal });
    // Consume within the deadline and forward cookies without caching or hop-by-hop headers.
    const content = await res.arrayBuffer();
    const outgoing = new Headers({ 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' });
    const contentType = res.headers.get('content-type');
    if (contentType) outgoing.set('content-type', contentType);
    for (const cookie of res.headers.getSetCookie()) outgoing.append('set-cookie', cookie);
    return new NextResponse(res.status === 204 || res.status === 304 ? null : content, { status: res.status, headers: outgoing });
  } catch {
    return NextResponse.json({ detail: controller.signal.aborted ? 'API request timed out' : 'API unavailable. Check that the backend is running.' }, { status: controller.signal.aborted ? 504 : 502 });
  } finally {
    clearTimeout(timer);
    controller.signal.removeEventListener('abort', abortRead);
    reader?.releaseLock();
  }
}

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const PATCH = proxy;
export const DELETE = proxy;
