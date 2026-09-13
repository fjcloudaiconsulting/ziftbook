// Runs only for "/" (wrangler.jsonc run_worker_first): send visitors to their language.
// Everything else is served straight from static assets, where _headers applies.
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

export default {
  async fetch(request, env) {
    if (new URL(request.url).pathname !== "/") return env.ASSETS.fetch(request);
    const language = pickLanguage(request.headers.get("Accept-Language"));
    return new Response(null, {
      status: 302,
      headers: { Location: `/${language}/`, "Cache-Control": "no-store", Vary: "Accept-Language" },
    });
  },
};
