import { onebrainApiBaseUrl } from "@/lib/onebrain-api";

type RouteContext = {
  params: Promise<{ path: string[] }>;
};

const HOP_BY_HOP_HEADERS = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
]);

function upstreamUrl(path: string[], search: string): string {
  const cleanPath = path.map(encodeURIComponent).join("/");
  return `${onebrainApiBaseUrl()}/api/${cleanPath}${search}`;
}

function forwardedHeaders(request: Request): Headers {
  const headers = new Headers();
  const authorization = request.headers.get("authorization");
  const cookie = request.headers.get("cookie");
  const contentType = request.headers.get("content-type");
  const callbackKeyId = request.headers.get("x-onebrain-callback-key-id");
  if (authorization) {
    headers.set("authorization", authorization);
  }
  if (cookie) {
    headers.set("cookie", cookie);
  }
  if (contentType) {
    headers.set("content-type", contentType);
  }
  if (callbackKeyId) {
    headers.set("x-onebrain-callback-key-id", callbackKeyId);
  }
  return headers;
}

function responseHeaders(upstream: Response): Headers {
  const headers = new Headers();
  upstream.headers.forEach((value, key) => {
    if (!HOP_BY_HOP_HEADERS.has(key.toLowerCase())) {
      headers.set(key, value);
    }
  });
  return headers;
}

async function proxy(request: Request, context: RouteContext): Promise<Response> {
  const { path } = await context.params;
  const incoming = new URL(request.url);
  const init: RequestInit = {
    method: request.method,
    headers: forwardedHeaders(request),
    cache: "no-store",
  };

  if (request.method !== "GET" && request.method !== "HEAD") {
    init.body = await request.arrayBuffer();
  }

  const upstream = await fetch(upstreamUrl(path, incoming.search), init);
  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: responseHeaders(upstream),
  });
}

export function GET(request: Request, context: RouteContext) {
  return proxy(request, context);
}

export function POST(request: Request, context: RouteContext) {
  return proxy(request, context);
}

export function PATCH(request: Request, context: RouteContext) {
  return proxy(request, context);
}

export function DELETE(request: Request, context: RouteContext) {
  return proxy(request, context);
}
