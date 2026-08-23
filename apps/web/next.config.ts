import type { NextConfig } from "next";

const privateLanDevOrigins = [
  "10.*.*.*",
  "192.168.*.*",
  ...Array.from({ length: 16 }, (_, index) => `172.${index + 16}.*.*`),
];

const configuredDevOrigins = (process.env.HZCU_ALLOWED_DEV_ORIGINS ?? "")
  .split(",")
  .map((origin) => origin.trim())
  .filter(Boolean);

const nextConfig: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  // Next.js protects development assets and HMR from cross-origin access.
  // Listening on 0.0.0.0 therefore also needs the phone-visible LAN address
  // to be allowlisted. The launcher supplies exact interface addresses; the
  // private ranges keep direct `npm run dev -- --hostname 0.0.0.0` usable.
  allowedDevOrigins: Array.from(
    new Set([
      "127.0.0.1",
      "localhost",
      ...privateLanDevOrigins,
      ...configuredDevOrigins,
    ]),
  ),
  async rewrites() {
    return [
      {
        source: "/api/v1/:path*",
        destination: `${process.env.HZCU_API_INTERNAL_URL ?? "http://127.0.0.1:8000"}/api/v1/:path*`,
      },
    ];
  },
};

export default nextConfig;
