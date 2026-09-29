import type { NextConfig } from "next";

// The browser only talks to this server; /api/* is proxied to the FastAPI backend.
const apiUrl = process.env.API_URL ?? "http://api:8000";

const nextConfig: NextConfig = {
  output: "standalone",
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${apiUrl}/:path*` }];
  },
};

export default nextConfig;
