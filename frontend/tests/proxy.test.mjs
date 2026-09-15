// End-to-end checks against the production server (`next start`) with stub APIs standing in for FastAPI.
// Requires a build first: `pnpm build && pnpm test`.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { after, before, describe, test } from "node:test";

const WEB_DIR = new URL("..", import.meta.url).pathname;

function stubApi(version) {
  return new Promise((resolve) => {
    const server = createServer((req, res) => {
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({ status: "ok", version, path: req.url }));
    }).listen(0, "127.0.0.1", () => resolve(server));
  });
}

async function startWeb(apiUrl, port) {
  const child = spawn("node_modules/.bin/next", ["start", "-p", String(port), "-H", "127.0.0.1"], {
    cwd: WEB_DIR,
    env: { ...process.env, ZIF_API_URL: apiUrl },
    stdio: "ignore",
  });
  for (let attempt = 0; attempt < 100; attempt++) {
    try {
      await fetch(`http://127.0.0.1:${port}/en`, { redirect: "manual" });
      return child;
    } catch {
      await new Promise((r) => setTimeout(r, 200));
    }
  }
  child.kill();
  throw new Error("next start did not come up");
}

const stop = (child) => new Promise((resolve) => child.once("exit", resolve).kill());

describe("one build, ZIF_API_URL read at runtime", () => {
  let apiA, apiB;
  before(async () => {
    apiA = await stubApi("A");
    apiB = await stubApi("B");
  });
  after(() => {
    apiA.close();
    apiB.close();
  });

  for (const [name, getApi, port] of [
    ["A", () => apiA, 3201],
    ["B", () => apiB, 3202],
  ]) {
    test(`forwards /api to API ${name}`, async () => {
      const web = await startWeb(`http://127.0.0.1:${getApi().address().port}`, port);
      try {
        const response = await fetch(`http://127.0.0.1:${port}/api/healthz?probe=1`, { redirect: "manual" });
        assert.equal(response.status, 200);
        const body = await response.json();
        assert.equal(body.version, name);
        assert.equal(body.path, "/api/healthz?probe=1", "path and query forwarded unchanged");
      } finally {
        await stop(web);
      }
    });
  }
});

describe("locale routing", () => {
  let api, web;
  const port = 3203;
  before(async () => {
    api = await stubApi("C");
    web = await startWeb(`http://127.0.0.1:${api.address().port}`, port);
  });
  after(async () => {
    await stop(web);
    api.close();
  });

  test("/api is never redirected to a locale", async () => {
    const response = await fetch(`http://127.0.0.1:${port}/api/healthz`, {
      redirect: "manual",
      headers: { "Accept-Language": "pt-BR" },
    });
    assert.equal(response.status, 200);
  });

  test("/ redirects to the visitor's language", async () => {
    const response = await fetch(`http://127.0.0.1:${port}/`, {
      redirect: "manual",
      headers: { "Accept-Language": "pt-BR" },
    });
    assert.equal(response.status, 307);
    assert.equal(new URL(response.headers.get("location"), "http://x").pathname, "/pt");
  });

  test("no response sets a cookie", async () => {
    for (const path of ["/", "/en", "/nl", "/pt", "/api/healthz"]) {
      const response = await fetch(`http://127.0.0.1:${port}${path}`, { redirect: "manual" });
      assert.equal(response.headers.get("set-cookie"), null, `${path} set a cookie`);
    }
  });
});

describe("account pages", () => {
  let api, web;
  const port = 3204;
  const origin = `http://127.0.0.1:${port}`;
  before(async () => {
    api = await stubApi("D");
    web = await startWeb(`http://127.0.0.1:${api.address().port}`, port);
  });
  after(async () => {
    await stop(web);
    api.close();
  });

  const pages = ["/en", "/en/sign-in", "/en/sign-up", "/en/sign-up/complete", "/en/forgot-password", "/nl/reset-password"];

  test("no page sends a Referer or can be framed", async () => {
    for (const path of pages) {
      const response = await fetch(`${origin}${path}`, { redirect: "manual" });
      assert.equal(response.status, 200, path);
      assert.equal(response.headers.get("referrer-policy"), "no-referrer", `${path} Referrer-Policy`);
      assert.match(response.headers.get("content-security-policy") ?? "", /frame-ancestors 'none'/, `${path} CSP`);
    }
  });

  test("a link page without JavaScript explains itself and has no form to submit", async () => {
    for (const [path, words] of [
      ["/en/sign-up/complete", "This page needs JavaScript to keep your link private."],
      ["/nl/reset-password", "Deze pagina heeft JavaScript nodig om je link privé te houden."],
    ]) {
      const html = await (await fetch(`${origin}${path}`)).text();
      // Inside the element: the page's message catalogue also carries the words.
      const noscript = html.match(/<noscript>([\s\S]*?)<\/noscript>/)?.[1] ?? "";
      assert.ok(noscript.includes(words), `${path} noscript message`);
      assert.doesNotMatch(html, /<form|type="password"/, `${path} renders a form before it has read its link`);
    }
  });
});
