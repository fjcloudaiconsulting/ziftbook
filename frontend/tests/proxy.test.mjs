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
      res.end(JSON.stringify({ status: "ok", version, path: req.url, headers: req.headers }));
    }).listen(0, "127.0.0.1", () => resolve(server));
  });
}

async function startWeb(apiUrl, port, env = {}) {
  const child = spawn("node_modules/.bin/next", ["start", "-p", String(port), "-H", "127.0.0.1"], {
    cwd: WEB_DIR,
    // undefined values are skipped by spawn, so ZIF_CLIENT_IP_HEADER defaults to unset even if a
    // developer's shell happens to export it.
    env: { ...process.env, ZIF_API_URL: apiUrl, ZIF_CLIENT_IP_HEADER: undefined, ...env },
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

  const pages = [
    "/en",
    "/en/sign-in",
    "/en/sign-up",
    "/en/sign-up/complete",
    "/en/forgot-password",
    "/nl/reset-password",
    "/pt/invite",
    "/en/calendar",
    "/nl/clients",
    "/pt/settings",
    "/en/opening-hours",
    "/en/team",
    "/nl/team/00000000-0000-0000-0000-000000000000",
    "/pt/my-hours",
  ];

  test("no page sends a Referer or can be framed", async () => {
    for (const path of pages) {
      const response = await fetch(`${origin}${path}`, { redirect: "manual" });
      assert.equal(response.status, 200, path);
      assert.equal(response.headers.get("referrer-policy"), "no-referrer", `${path} Referrer-Policy`);
      assert.match(response.headers.get("content-security-policy") ?? "", /frame-ancestors 'none'/, `${path} CSP`);
    }
  });

  test("forms post, so a submit before the page runs never puts a password in the address bar", async () => {
    for (const path of ["/en/sign-in", "/en/sign-up", "/en/forgot-password"]) {
      const html = await (await fetch(`${origin}${path}`)).text();
      const forms = html.match(/<form[^>]*>/g) ?? [];
      assert.equal(forms.length, 1, `${path} has one form`);
      assert.match(forms[0], /method="post"/, `${path} form method`);
    }
  });

  test("a link page without JavaScript explains itself and has no form to submit", async () => {
    for (const [path, words] of [
      ["/en/sign-up/complete", "This page needs JavaScript to keep your link private."],
      ["/nl/reset-password", "Deze pagina heeft JavaScript nodig om je link privé te houden."],
      ["/pt/invite", "Esta página precisa de JavaScript para manter seu link privado."],
    ]) {
      const html = await (await fetch(`${origin}${path}`)).text();
      // Inside the element: the page's message catalogue also carries the words.
      const noscript = html.match(/<noscript>([\s\S]*?)<\/noscript>/)?.[1] ?? "";
      assert.ok(noscript.includes(words), `${path} noscript message`);
      assert.doesNotMatch(html, /<form|type="password"/, `${path} renders a form before it has read its link`);
    }
  });
});

describe("the visitor's address", () => {
  let api, unset, withHeader;
  const forged = {
    "X-Forwarded-For": "198.51.100.1",
    "X-Real-IP": "198.51.100.2",
    Forwarded: "for=198.51.100.3",
    "X-Forwarded-Proto": "https",
  };

  before(async () => {
    api = await stubApi("E");
    const target = `http://127.0.0.1:${api.address().port}`;
    unset = await startWeb(target, 3205);
    withHeader = await startWeb(target, 3206, { ZIF_CLIENT_IP_HEADER: "cf-connecting-ip" });
  });
  after(async () => {
    await stop(unset);
    await stop(withHeader);
    api.close();
  });

  test("unset: nothing the client sent reaches the API", async () => {
    const response = await fetch("http://127.0.0.1:3205/api/healthz", {
      headers: { ...forged, "CF-Connecting-IP": "203.0.113.7" },
    });
    const { headers } = await response.json();

    for (const name of ["x-forwarded-for", "x-real-ip", "forwarded", "x-forwarded-proto"]) {
      assert.equal(headers[name], undefined, name);
    }
  });

  test("set: the trusted header's value is forwarded as X-Forwarded-For alone", async () => {
    const response = await fetch("http://127.0.0.1:3206/api/healthz", {
      headers: { ...forged, "CF-Connecting-IP": "203.0.113.7" },
    });
    const { headers } = await response.json();

    assert.equal(headers["x-forwarded-for"], "203.0.113.7");
    assert.equal(headers["x-real-ip"], undefined);
    assert.equal(headers["forwarded"], undefined);
  });

  for (const [label, value] of [
    ["not an IP", "nope"],
    ["a list", "203.0.113.7, 198.51.100.9"],
    ["missing", undefined],
  ]) {
    test(`set: ${label} forwards no X-Forwarded-For`, async () => {
      const headers = value === undefined ? { ...forged } : { ...forged, "CF-Connecting-IP": value };
      const response = await fetch("http://127.0.0.1:3206/api/healthz", { headers });
      const { headers: seen } = await response.json();

      assert.equal(seen["x-forwarded-for"], undefined);
    });
  }

  test("set: an IPv4-mapped address loses its ::ffff: prefix", async () => {
    const response = await fetch("http://127.0.0.1:3206/api/healthz", {
      headers: { ...forged, "CF-Connecting-IP": "::ffff:203.0.113.8" },
    });
    const { headers } = await response.json();

    assert.equal(headers["x-forwarded-for"], "203.0.113.8");
  });
});
