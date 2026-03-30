import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // API 프록시 (CORS 우회)
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: "http://localhost:8080/api/:path*",
      },
    ];
  },
};

export default nextConfig;
