import type { NextConfig } from "next";

// Production: a static export that `leak serve` (FastAPI) serves from web/out,
// so there is one local process and no Node server at runtime.
// Development: `npm run dev` proxies /api to a running `leak serve`.
const isDev = process.env.NODE_ENV === "development";
const api = process.env.LEAK_API ?? "http://127.0.0.1:8765";

const config: NextConfig = isDev
  ? { async rewrites() { return [{ source: "/api/:path*", destination: `${api}/api/:path*` }]; } }
  : {
      output: "export", // web/out, copied into leak/_web by scripts/copy-to-package.mjs
      images: { unoptimized: true },
      trailingSlash: true,
    };

export default config;
