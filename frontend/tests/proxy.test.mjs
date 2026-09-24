// End-to-end checks against the production server (`next start`) with stub APIs standing in for FastAPI.
// Requires a build first: `pnpm build && pnpm test`.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createServer, request as httpRequest } from "node:http";
import { connect } from "node:net";
import { after, before, describe, test } from "node:test";
import { gzipSync } from "node:zlib";

const WEB_DIR = new URL("..", import.meta.url).pathname;

function stubApi(version) {
  return new Promise((resolve) => {
    const server = createServer((req, res) => {
      // Echoes the request's own x-request-id back as a response header (E1): lets a test check
      // the id the proxy forwarded without depending on its random value.
      if (req.headers["x-request-id"]) res.setHeader("X-Request-ID", req.headers["x-request-id"]);
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({ status: "ok", version, path: req.url, headers: req.headers }));
    }).listen(0, "127.0.0.1", () => resolve(server));
  });
}

function stubGzipCookies(version) {
  return new Promise((resolve) => {
    const server = createServer((req, res) => {
      const body = gzipSync(Buffer.from(JSON.stringify({ status: "ok", version })));
      res.setHeader("Content-Type", "application/json");
      res.setHeader("Content-Encoding", "gzip");
      res.setHeader("Set-Cookie", ["a=1", "b=2"]);
      res.end(body);
    }).listen(0, "127.0.0.1", () => resolve(server));
  });
}

function stubEcho() {
  return new Promise((resolve) => {
    const server = createServer((req, res) => {
      const chunks = [];
      req.on("data", (chunk) => chunks.push(chunk));
      req.on("end", () => {
        res.setHeader("Content-Type", "application/json");
        res.end(JSON.stringify({ method: req.method, body: Buffer.concat(chunks).toString("utf8") }));
      });
    }).listen(0, "127.0.0.1", () => resolve(server));
  });
}

function stubHopByHopResponse(version) {
  return new Promise((resolve) => {
    const server = createServer((req, res) => {
      res.setHeader("Content-Type", "application/json");
      res.setHeader("Proxy-Connection", "x");
      res.setHeader("Trailer", "t");
      res.end(JSON.stringify({ status: "ok", version }));
    }).listen(0, "127.0.0.1", () => resolve(server));
  });
}

function stubRedirect(location) {
  return new Promise((resolve) => {
    const server = createServer((req, res) => {
      res.writeHead(302, { Location: location });
      res.end();
    }).listen(0, "127.0.0.1", () => resolve(server));
  });
}

// Sends Expect: 100-continue, which fetch()/undici's client side refuses to let a caller set
// (forbidden request header); node:http has no such restriction.
function postWithExpectContinue(port, path, payload) {
  return new Promise((resolve, reject) => {
    const req = httpRequest(
      { host: "127.0.0.1", port, path, method: "POST", headers: { Expect: "100-continue", "Content-Type": "application/json", "Content-Length": Buffer.byteLength(payload) } },
      (res) => {
        const chunks = [];
        res.on("data", (chunk) => chunks.push(chunk));
        res.on("end", () => resolve({ status: res.statusCode, body: Buffer.concat(chunks).toString("utf8") }));
      },
    );
    req.on("error", reject);
    req.end(payload);
  });
}

// A raw GET with arbitrary headers, written straight to the socket: fetch()'s client side refuses
// to let a caller set some of these (Connection, like Expect), and node:http's own client
// special-cases and rejects a bare Trailer header outside chunked transfer.
function getWithHeaders(port, path, headers) {
  return new Promise((resolve, reject) => {
    const socket = connect(port, "127.0.0.1", () => {
      const lines = [`GET ${path} HTTP/1.1`, `Host: 127.0.0.1:${port}`];
      for (const [name, value] of Object.entries(headers)) lines.push(`${name}: ${value}`);
      socket.write(lines.join("\r\n") + "\r\n\r\n");
    });
    let raw = "";
    socket.on("data", (chunk) => {
      raw += chunk;
      // A keep-alive connection never sends 'end' on its own, and the proxy may reply chunked
      // (a streamed fetch body) rather than with a Content-Length, so stop as soon as whichever
      // terminator applies has arrived (every response here is a small JSON body).
      const split = raw.indexOf("\r\n\r\n");
      if (split === -1) return;
      const head = raw.slice(0, split);
      const rest = raw.slice(split + 4);
      const chunked = /transfer-encoding: chunked/i.test(head);
      const contentLength = Number(/content-length: (\d+)/i.exec(head)?.[1] ?? -1);
      const done = chunked ? rest.endsWith("0\r\n\r\n") : contentLength >= 0 && Buffer.byteLength(rest) >= contentLength;
      if (!done) return;
      socket.destroy();
      const body = chunked ? rest.split("\r\n").filter((_, i) => i % 2 === 1).join("") : rest;
      resolve({ status: Number(head.split(" ")[1]), body });
    });
    socket.on("error", reject);
  });
}

function parsedLogLines(getText, msg) {
  return getText()
    .split("\n")
    .filter((line) => line.startsWith("{"))
    .map((line) => {
      try {
        return JSON.parse(line);
      } catch {
        return null;
      }
    })
    .filter((parsed) => parsed && parsed.msg === msg);
}

// Poll a growing text buffer (from startWeb's child.logs) for a JSON line with the given msg.
// Never counts total lines: Next prints its own banner, which never starts with "{". After the
// first match, wait a bit and re-read once more before the caller counts, so a second line
// written just after (a duplicate) is caught rather than raced.
async function waitForLog(getText, msg, timeoutMs = 5000) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const lines = parsedLogLines(getText, msg);
    if (lines.length > 0) {
      await new Promise((r) => setTimeout(r, 200));
      return parsedLogLines(getText, msg);
    }
    if (Date.now() > deadline) return [];
    await new Promise((r) => setTimeout(r, 50));
  }
}

async function startWeb(apiUrl, port, env = {}) {
  const child = spawn("node_modules/.bin/next", ["start", "-p", String(port), "-H", "127.0.0.1"], {
    cwd: WEB_DIR,
    // undefined values are skipped by spawn, so ZIF_CLIENT_IP_HEADER defaults to unset even if a
    // developer's shell happens to export it. ZIF_LOG_LEVEL/FORMAT are pinned the same way, unless
    // a test overrides them.
    env: { ...process.env, ZIF_API_URL: apiUrl, ZIF_CLIENT_IP_HEADER: undefined, ZIF_LOG_LEVEL: "INFO", ZIF_LOG_FORMAT: "json", ...env },
  });
  let stdout = "";
  let stderr = "";
  child.stdout.on("data", (chunk) => {
    stdout += chunk;
  });
  child.stderr.on("data", (chunk) => {
    stderr += chunk;
  });
  child.logs = { stdout: () => stdout, stderr: () => stderr };
  for (let attempt = 0; attempt < 100; attempt++) {
    try {
      await fetch(`http://127.0.0.1:${port}/en`, { redirect: "manual" });
      return child;
    } catch {
      if (child.exitCode !== null) throw new Error(`next start exited early (${child.exitCode}): ${stderr}`);
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
    "/en/services",
    "/nl/services/new",
    "/pt/services/00000000-0000-0000-0000-000000000000",
    "/en/team/00000000-0000-0000-0000-000000000000/time-off",
    "/nl/my-hours/time-off",
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

describe("request id forwarding (E1)", () => {
  let api, web;
  const port = 3207;
  const HEX32 = /^[0-9a-f]{32}$/;

  before(async () => {
    api = await stubApi("F");
    web = await startWeb(`http://127.0.0.1:${api.address().port}`, port);
  });
  after(async () => {
    await stop(web);
    api.close();
  });

  test("no x-request-id sent: upstream sees 32 hex, response echoes it", async () => {
    const response = await fetch(`http://127.0.0.1:${port}/api/healthz`);
    const body = await response.json();
    assert.match(body.headers["x-request-id"], HEX32);
    assert.equal(response.headers.get("x-request-id"), body.headers["x-request-id"]);
  });

  test("a valid x-request-id is forwarded unchanged", async () => {
    const response = await fetch(`http://127.0.0.1:${port}/api/healthz`, { headers: { "x-request-id": "abc-1" } });
    const body = await response.json();
    assert.equal(body.headers["x-request-id"], "abc-1");
    assert.equal(response.headers.get("x-request-id"), "abc-1");
  });

  test("an invalid x-request-id is replaced with 32 hex, never the raw value", async () => {
    const response = await fetch(`http://127.0.0.1:${port}/api/healthz`, { headers: { "x-request-id": "a b" } });
    const body = await response.json();
    assert.match(body.headers["x-request-id"], HEX32);
    assert.notEqual(body.headers["x-request-id"], "a b");
  });
});

describe("dead upstream (E2)", () => {
  const port = 3208;

  test("502 with one clean error line, no leaked query, no rewrite header", async () => {
    // A port obtained by listening on 0 and closing again: guaranteed refused, never port 9
    // (which fetch itself blocks).
    const deadServer = createServer().listen(0, "127.0.0.1");
    await new Promise((resolve) => deadServer.once("listening", resolve));
    const deadPort = deadServer.address().port;
    await new Promise((resolve) => deadServer.close(resolve));

    const web = await startWeb(`http://127.0.0.1:${deadPort}`, port);
    try {
      const response = await fetch(`http://127.0.0.1:${port}/api/x?email=a%40b.c`, { redirect: "manual" });
      assert.equal(response.status, 502);
      assert.deepEqual(await response.json(), { code: "api_unavailable" });
      assert.equal(response.headers.get("x-middleware-rewrite"), null);

      const requestIdHeader = response.headers.get("x-request-id");
      const lines = await waitForLog(web.logs.stdout, "api unavailable");
      assert.equal(lines.length, 1, "exactly one parsed 'api unavailable' line");
      assert.equal(lines[0].level, "ERROR");
      assert.equal(lines[0].request_id, requestIdHeader);
      assert.ok(lines[0].exc.some((link) => link.code === "ECONNREFUSED"));

      assert.doesNotMatch(web.logs.stdout(), /email/);
      assert.doesNotMatch(web.logs.stderr(), /email/);
    } finally {
      await stop(web);
    }
  });
});

// Guard, not a fence: on Next 16.3.5 Headers iteration already splits multiple Set-Cookie apart
// (see proxy.ts) and the runtime already serves an already-decoded body without a stale
// content-encoding, so neither half of this can be forced red against a wrong implementation in
// this stack. Kept to catch a regression if that runtime behavior ever changes.
describe("response headers passthrough (E3, guard)", () => {
  const port = 3209;

  test("multiple Set-Cookie reach the client; a gzipped upstream body arrives already decoded", async () => {
    const api = await stubGzipCookies("G");
    const web = await startWeb(`http://127.0.0.1:${api.address().port}`, port);
    try {
      const response = await fetch(`http://127.0.0.1:${port}/api/x`, { redirect: "manual" });
      assert.deepEqual(response.headers.getSetCookie().sort(), ["a=1", "b=2"]);
      const body = await response.json();
      assert.equal(body.version, "G");
    } finally {
      await stop(web);
      api.close();
    }
  });
});

describe("body and redirects (E4)", () => {
  const port = 3212;

  test("Expect: 100-continue: 200, body echoed byte-exact with its method", async () => {
    const api = await stubEcho();
    const web = await startWeb(`http://127.0.0.1:${api.address().port}`, port);
    try {
      const payload = JSON.stringify({ a: 1, b: "two" });
      const { status, body } = await postWithExpectContinue(port, "/api/echo", payload);
      assert.equal(status, 200);
      assert.deepEqual(JSON.parse(body), { method: "POST", body: payload });
    } finally {
      await stop(web);
      api.close();
    }
  });

  test("a 302 with an absolute Location comes back as a 302 with that Location, not followed", async () => {
    const target = "https://example.com/somewhere";
    const api = await stubRedirect(target);
    const web = await startWeb(`http://127.0.0.1:${api.address().port}`, port + 1);
    try {
      const response = await fetch(`http://127.0.0.1:${port + 1}/api/x`, { redirect: "manual" });
      assert.equal(response.status, 302);
      assert.equal(response.headers.get("location"), target);
    } finally {
      await stop(web);
      api.close();
    }
  });
});

describe("bad ZIF_LOG_LEVEL at startup (E5)", () => {
  test("next start exits non-zero within the startup wait; stderr names ZIF_LOG_LEVEL, not the value", async () => {
    const child = spawn("node_modules/.bin/next", ["start", "-p", "3210", "-H", "127.0.0.1"], {
      cwd: WEB_DIR,
      env: { ...process.env, ZIF_API_URL: "http://127.0.0.1:1", ZIF_CLIENT_IP_HEADER: undefined, ZIF_LOG_LEVEL: "nope", ZIF_LOG_FORMAT: "json" },
    });
    let stderr = "";
    child.stderr.on("data", (chunk) => (stderr += chunk));

    const exitCode = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        child.kill();
        reject(new Error("next start did not exit within the startup wait"));
      }, 5000);
      child.once("exit", (code) => {
        clearTimeout(timer);
        resolve(code);
      });
    });

    assert.notEqual(exitCode, 0);
    assert.match(stderr, /ZIF_LOG_LEVEL/);
    assert.doesNotMatch(stderr, /nope/);
  });
});

describe("unset ZIF_API_URL (E7)", () => {
  test("502 api_unavailable plus one ERROR 'api url unset' line", async () => {
    const port = 3211;
    const web = await startWeb(undefined, port);
    try {
      const response = await fetch(`http://127.0.0.1:${port}/api/x`, { redirect: "manual" });
      assert.equal(response.status, 502);
      assert.deepEqual(await response.json(), { code: "api_unavailable" });

      const lines = await waitForLog(web.logs.stdout, "api url unset");
      assert.equal(lines.length, 1);
      assert.equal(lines[0].level, "ERROR");
    } finally {
      await stop(web);
    }
  });
});

describe("hop-by-hop headers", () => {
  const port = 3213;

  test("Proxy-Authorization and Trailer never reach the upstream; X-Request-ID does", async () => {
    const api = await stubApi("H");
    const web = await startWeb(`http://127.0.0.1:${api.address().port}`, port);
    try {
      const { status, body } = await getWithHeaders(port, "/api/x", { "Proxy-Authorization": "p", Trailer: "t", "X-Request-ID": "r1" });
      assert.equal(status, 200);
      const { headers } = JSON.parse(body);
      assert.equal(headers["proxy-authorization"], undefined);
      assert.equal(headers["trailer"], undefined);
      assert.equal(headers["x-request-id"], "r1");
    } finally {
      await stop(web);
      api.close();
    }
  });

  // Guard: this is the exact shape of the bug the review found (an "@" isn't a legal header-name
  // character, so a naive headers.delete(name) on a token read out of Connection throws).
  test("guard: Connection: x@y never 500s the proxy", async () => {
    const api = await stubApi("I");
    const web = await startWeb(`http://127.0.0.1:${api.address().port}`, port + 1);
    try {
      const { status, body } = await getWithHeaders(port + 1, "/api/x", { Connection: "x@y" });
      assert.equal(status, 200);
      assert.equal(JSON.parse(body).version, "I");
    } finally {
      await stop(web);
      api.close();
    }
  });

  test("Proxy-Connection and Trailer from the upstream never reach the client", async () => {
    const api = await stubHopByHopResponse("J");
    const web = await startWeb(`http://127.0.0.1:${api.address().port}`, port + 2);
    try {
      // keep-alive/connection are excluded on purpose: Node's own HTTP server sets those itself.
      const response = await fetch(`http://127.0.0.1:${port + 2}/api/x`, { redirect: "manual" });
      assert.equal(response.status, 200);
      assert.equal(response.headers.get("proxy-connection"), null);
      assert.equal(response.headers.get("trailer"), null);
    } finally {
      await stop(web);
      api.close();
    }
  });
});
