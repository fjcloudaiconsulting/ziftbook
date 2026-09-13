// Runs for every request (wrangler.jsonc run_worker_first: true):
// 1. www and plain http go to https://ziftbook.com, 2. "/" goes to the visitor's language, 3. everything else is a static asset.
const APEX = "ziftbook.com";
const HSTS = "max-age=31536000; includeSubDomains";
const SUPPORTED = ["en", "nl", "pt"];

export function pickLanguage(acceptLanguage) {
  const ranked = (acceptLanguage ?? "")
    .split(",")
    .map((part, index) => {
      const [tag, ...params] = part.trim().split(";");
      const weight = params.find((p) => p.trim().startsWith("q="));
      const q = weight ? Number(weight.trim().slice(2)) : 1;
      return { language: tag.trim().toLowerCase().split("-")[0], q, index };
    })
    .filter((entry) => entry.q > 0)
    .sort((a, b) => b.q - a.q || a.index - b.index);
  return ranked.find((entry) => SUPPORTED.includes(entry.language))?.language ?? "en";
}

function redirect(location, status, headers = {}) {
  return new Response(null, {
    status,
    headers: { Location: location, "Strict-Transport-Security": HSTS, ...headers },
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const production = url.hostname === APEX || url.hostname === `www.${APEX}`;

    if (production && (url.hostname !== APEX || url.protocol !== "https:")) {
      return redirect(`https://${APEX}${url.pathname}${url.search}`, 301);
    }
    if (url.pathname === "/") {
      const language = pickLanguage(request.headers.get("Accept-Language"));
      return redirect(`/${language}/`, 302, { "Cache-Control": "no-store", Vary: "Accept-Language" });
    }
    return env.ASSETS.fetch(request);
  },
};
