import type { NextConfig } from "next";

// Static export so the same bundle can be (a) mounted as static files by the
// Zeroth FastAPI app under /console, and (b) hosted standalone. basePath pins a
// single mount subpath so asset URLs and the auth-bypass prefix line up in both
// modes. No SSR/server actions exist under `output: "export"` — every data view
// is a client component that fetches at runtime against a configurable API base.
const nextConfig: NextConfig = {
  output: "export",
  basePath: "/console",
  trailingSlash: true,
  images: { unoptimized: true },
};

export default nextConfig;
