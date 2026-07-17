import type { NextConfig } from "next";

const apiProxyUrl = process.env.ONEBRAIN_API_PROXY_URL?.replace(/\/$/, "");

const nextConfig: NextConfig = {
  reactStrictMode: true,
  async rewrites() {
    return apiProxyUrl
      ? [{ source: "/api/:path*", destination: `${apiProxyUrl}/api/:path*` }]
      : [];
  },
};

export default nextConfig;
