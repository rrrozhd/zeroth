import { writeFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

function exactHttpOrigin(value) {
  if (typeof value !== "string" || value.length === 0 || /[\r\n]/.test(value)) {
    throw new Error("standalone API base must be an exact HTTP(S) origin");
  }
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    throw new Error("standalone API base must be an exact HTTP(S) origin");
  }
  if (
    !["http:", "https:"].includes(parsed.protocol) ||
    parsed.username ||
    parsed.password ||
    parsed.hostname.includes("*") ||
    parsed.pathname !== "/" ||
    parsed.search ||
    parsed.hash ||
    parsed.origin !== value
  ) {
    throw new Error("standalone API base must be an exact HTTP(S) origin");
  }
  return parsed.origin;
}

export function renderStandaloneNginx(apiOrigin) {
  const origin = exactHttpOrigin(apiOrigin);
  const csp = [
    "default-src 'self'",
    "base-uri 'self'",
    "object-src 'none'",
    "frame-ancestors 'none'",
    "form-action 'self'",
    "img-src 'self' data:",
    "font-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "script-src 'self' 'unsafe-inline'",
    `connect-src 'self' ${origin}`,
  ].join("; ");
  return `server {
  listen 8080;
  server_name _;
  root /usr/share/nginx/html;

  add_header Content-Security-Policy "${csp}" always;
  add_header Referrer-Policy "no-referrer" always;
  add_header X-Content-Type-Options "nosniff" always;
  add_header X-Frame-Options "DENY" always;
  add_header Permissions-Policy "camera=(), microphone=(), geolocation=()" always;

  location = / { return 308 /console/; }
  location = /console { return 308 /console/; }
  location /console/ {
    try_files $uri $uri/ $uri/index.html =404;
  }
}
`;
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  const [, , apiOrigin, output] = process.argv;
  if (!output) throw new Error("usage: render-standalone-nginx.mjs API_ORIGIN OUTPUT");
  writeFileSync(output, renderStandaloneNginx(apiOrigin), { encoding: "utf8", mode: 0o644 });
}
