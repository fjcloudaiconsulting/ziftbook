// ZIF-148: unknown addresses answer 404 (never a streamed 200) with the not-found page in the
// address's language. Runs the production build's server, so `pnpm build` first (CI and the
// frontend gate do); skipped when there is no build.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import http from "node:http";
import { setTimeout as sleep } from "node:timers/promises";
import { after, before, describe, test } from "node:test";
import { fileURLToPath } from "node:url";

const SERVER = fileURLToPath(new URL("../.next/standalone/server.js", import.meta.url));

const TITLES = { en: "Page not found", nl: "Pagina niet gevonden", pt: "Página não encontrada" };

async function freePort() {
  const probe = http.createServer();
  await new Promise((resolve) => probe.listen(0, "127.0.0.1", resolve));
  const { port } = probe.address();
  await new Promise((resolve) => probe.close(resolve));
  return port;
}

describe("not-found page (ZIF-148)", { skip: !existsSync(SERVER) && "no production build" }, () => {
  let api;
  let web;
  let base;

  before(async () => {
    // Stand-in API: setup is done and every booking page slug is unknown. The backend answers an
    // unpublished page byte for byte like an unknown slug (test_booking_page_api.py).
    api = http.createServer((request, response) => {
      if (request.url === "/api/setup") return response.end('{"needs_setup":false}');
      response.writeHead(404).end('{"code":"not_found"}');
    });
    await new Promise((resolve) => api.listen(0, "127.0.0.1", resolve));
    const port = await freePort();
    web = spawn(process.execPath, [SERVER], {
      env: { ...process.env, PORT: String(port), HOSTNAME: "127.0.0.1", ZIF_API_URL: `http://127.0.0.1:${api.address().port}` },
      stdio: "ignore",
    });
    base = `http://127.0.0.1:${port}`;
    for (let tries = 0; ; tries++) {
      try {
        await fetch(`${base}/en/sign-in`);
        break;
      } catch (error) {
        if (tries > 300) throw error;
        await sleep(100);
      }
    }
  });

  after(() => {
    web?.kill();
    api?.close();
  });

  test("fence: unknown slugs and unknown addresses of any depth answer 404 with the localized title", async () => {
    // Wrong implementations killed: no catch-all (two or more segments get Next's English page), a
    // loading.tsx above the pages (the 404 streams as a 200), no app/[locale]/not-found.tsx.
    for (const [locale, title] of Object.entries(TITLES)) {
      for (const path of [`/${locale}/nope-studio`, `/${locale}/booking/x/y`, `/${locale}/a/b/c`]) {
        const response = await fetch(base + path, { redirect: "manual" });
        assert.equal(response.status, 404, path);
        assert.ok((await response.text()).includes(`<title>${title} · ziftbook</title>`), `${path}: title`);
      }
    }
  });

  test("file-like addresses without a language keep Next's own 404, never a 500", async () => {
    for (const path of ["/wp-login.php", "/wp-admin/setup-config.php", "/wp-content/plugins/x/readme.txt"]) {
      const response = await fetch(base + path, { redirect: "manual" });
      assert.equal(response.status, 404, path);
      await response.arrayBuffer();
    }
  });

  test("an address without a language is still sent to one first", async () => {
    const response = await fetch(`${base}/studio-lumen`, { redirect: "manual" });
    assert.equal(response.status, 307);
    assert.match(response.headers.get("location") ?? "", /\/en\/studio-lumen$/);
  });
});
