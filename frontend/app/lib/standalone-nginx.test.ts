import { describe, expect, it } from "vitest";

import { renderStandaloneNginx } from "../../scripts/render-standalone-nginx.mjs";

describe("standalone console nginx renderer", () => {
  it("pins connect-src to self and one exact API origin", () => {
    const rendered = renderStandaloneNginx("https://api.example.test:8443");

    expect(rendered).toContain(
      "connect-src 'self' https://api.example.test:8443",
    );
    expect(rendered).toContain('add_header Content-Security-Policy "');
    expect(rendered).toContain("try_files $uri $uri/ $uri/index.html =404;");
  });

  it.each([
    "",
    "*",
    "https://*.example.test",
    "https://user:pass@example.test",
    "https://api.example.test/path",
    "https://api.example.test?query=1",
    "https://api.example.test/#fragment",
    "javascript:alert(1)",
    "https://api.example.test\nadd_header X-Bad injected",
  ])("rejects non-exact or injectable origin %j", (origin) => {
    expect(() => renderStandaloneNginx(origin)).toThrow(/exact HTTP\(S\) origin/);
  });
});
