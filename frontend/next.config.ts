import type { NextConfig } from "next";
import createNextIntlPlugin from "next-intl/plugin";

const nextConfig: NextConfig = {
  output: "standalone",
  // The app serves no optimized images, so /_next/image and sharp stay unused at runtime.
  images: { unoptimized: true },
  poweredByHeader: false,
  experimental: {
    // Next keeps a copy of every proxied request body up to this size and ends it there as if
    // complete. Down from 10MB: the proxy forwards at most MAX_BODY_BYTES, or WEBHOOK_MAX_BODY_BYTES for
    // Stripe's webhook (lib/upstream.ts), so this only has to stay a socket read (64 KiB) above the
    // larger for a cut body to be refused, never forwarded.
    proxyClientMaxBodySize: 1024 * 1024,
  },
  async headers() {
    return [
      {
        // Every page: an emailed link's token must not leak through a Referer, no other site may frame
        // the account forms, HTTPS is kept for a year, and a body is never sniffed into a script. Payment
        // is delegated to Stripe's frames only; popups we open keep working but cannot reach back
        // (COOP). /api answers carry the API's own headers.
        source: "/((?!api/).*)",
        headers: [
          { key: "Referrer-Policy", value: "no-referrer" },
          { key: "Strict-Transport-Security", value: "max-age=31536000; includeSubDomains" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          {
            key: "Permissions-Policy",
            value: 'camera=(), microphone=(), geolocation=(), payment=(self "https://js.stripe.com")',
          },
          { key: "Cross-Origin-Opener-Policy", value: "same-origin-allow-popups" },
          { key: "Content-Security-Policy", value: "frame-ancestors 'none'" },
        ],
      },
    ];
  },
};

export default createNextIntlPlugin()(nextConfig);
