import type { NextConfig } from "next";
import createNextIntlPlugin from "next-intl/plugin";

const nextConfig: NextConfig = {
  output: "standalone",
  experimental: {
    // Next keeps a copy of every proxied request body up to this size and ends it there as if
    // complete. Down from 10MB: the proxy forwards at most MAX_BODY_BYTES (lib/upstream.ts), so this
    // only has to stay a socket read (64 KiB) above that for a cut body to be refused, never forwarded.
    proxyClientMaxBodySize: 1024 * 1024,
  },
  async headers() {
    return [
      {
        // Every page: an emailed link's token must not leak through a Referer, and no other site may frame
        // the account forms. /api answers carry the API's own headers.
        source: "/((?!api/).*)",
        headers: [
          { key: "Referrer-Policy", value: "no-referrer" },
          { key: "Content-Security-Policy", value: "frame-ancestors 'none'" },
        ],
      },
    ];
  },
};

export default createNextIntlPlugin()(nextConfig);
