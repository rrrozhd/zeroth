import { createHash } from "node:crypto";

type RequestMetadata = {
  method: string;
  url: string;
  resourceType: string;
  postData?: string | null;
};

type ResponseMetadata = {
  url: string;
  status: number;
  resourceType: string;
};

export function sanitizeUrl(value: string): string {
  const url = new URL(value);
  const secretShape = /^(?:sk-(?:proj-)?[A-Za-z0-9_-]{20,}|Bearer[._~+/-]?[A-Za-z0-9._~+/-]{16,})$/i;
  const pathname = url.pathname
    .split("/")
    .map((segment) => {
      let decoded = segment;
      try {
        decoded = decodeURIComponent(segment);
      } catch {
        // Malformed escapes remain opaque; the length guard below still fails safe.
      }
      return secretShape.test(decoded) || decoded.length > 96 ? "[redacted]" : segment;
    })
    .join("/");
  return `${url.origin}${pathname}`;
}

export function summarizeRequest(request: RequestMetadata) {
  const summary: Record<string, string | number> = {
    method: request.method,
    url: sanitizeUrl(request.url),
    resource_type: request.resourceType,
  };
  if (request.postData !== undefined && request.postData !== null) {
    const body = Buffer.from(request.postData);
    summary.body_bytes = body.byteLength;
    summary.body_sha256 = createHash("sha256").update(body).digest("hex");
  }
  return summary;
}

export function summarizeResponse(response: ResponseMetadata) {
  return {
    url: sanitizeUrl(response.url),
    status: response.status,
    resource_type: response.resourceType,
  };
}
