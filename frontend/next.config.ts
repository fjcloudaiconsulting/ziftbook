import type { NextConfig } from "next";
import createNextIntlPlugin from "next-intl/plugin";

const nextConfig: NextConfig = {
  output: "standalone",
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
