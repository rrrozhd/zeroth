import type { NextConfig } from "next";

// Static export so the same bundle can be (a) mounted as static files by the
// Zeroth FastAPI app under /console, and (b) hosted standalone. basePath pins a
// single mount subpath so asset URLs and the auth-bypass prefix line up in both
// modes. No SSR/server actions exist under `output: "export"` — every data view
// is a client component that fetches at runtime against a configurable API base.
const nextConfig: NextConfig = {
  output: "export",
  basePath: "/console",
  // The persistent dev service is intentionally published on loopback. Allow
  // that browser origin so Next can serve its dev/HMR resources and hydrate
  // client components instead of leaving a static, non-interactive shell.
  allowedDevOrigins: ["127.0.0.1"],
  // The validation campaign traverses every published console route. Keep that
  // surface resident in the long-lived dev compiler so early routes are not
  // evicted and recompiled while later routes are still being exercised.
  onDemandEntries: {
    maxInactiveAge: 60 * 60 * 1000,
    pagesBufferLength: 64,
  },
  trailingSlash: true,
  images: { unoptimized: true },
};

export default nextConfig;
