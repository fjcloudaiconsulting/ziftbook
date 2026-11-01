// ZIF-236: where the pages' Content-Security-Policy sends its violation reports (ZIF_CSP_REPORT_URL).
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { describe, test } from "node:test";

import { cspFor, cspStartupFields, reportingDirectives, reportUrlFor, reportUrlOf } from "../lib/csp.ts";

const GOOD = "https://app.example.test/api/csp-reports";

// directive name -> its sources, as one space-separated string.
function directives(policy) {
  const parsed = {};
  for (const part of policy.split(";")) {
    const [name, ...sources] = part.trim().split(/\s+/);
    if (name) parsed[name] = sources.join(" ");
  }
  return parsed;
}

describe("reportUrlOf", () => {
  test("fence: only this API's report route on an https host, written exactly as a browser would", () => {
    // Wrong implementations killed: #360's https-only check, which passes a third-party collector (raw reports
    // carry a checkout's client secret) and a `;` or `,` that adds a directive or splits the policy in two.
    assert.equal(reportUrlOf(GOOD), GOOD);
    assert.equal(reportUrlOf("https://app.example.test:8443/api/csp-reports"), "https://app.example.test:8443/api/csp-reports");
    assert.equal(reportUrlOf("https://localhost/api/csp-reports"), "https://localhost/api/csp-reports");
    for (const value of [
      undefined,
      "",
      "not a url",
      "javascript:alert(1)",
      "http://app.example.test/api/csp-reports",
      "https://reports.example.test/csp",
      "https://sentry.example.test/api/1/security/?sentry_key=k",
      "https://app.example.test/api/csp-reports/",
      "https://app.example.test/api/csp-reports?",
      "https://app.example.test/api/csp-reports?x=1",
      "https://app.example.test/api/csp-reports#",
      "https://app.example.test/api/csp-reports#x",
      "https://app.example.test/API/CSP-REPORTS",
      "https://app.example.test/api/csp%2Dreports",
      "https://app.example.test\\api/csp-reports",
      "https://APP.example.test/api/csp-reports",
      "https://user:pw@app.example.test/api/csp-reports",
      "https://[::1]/api/csp-reports",
      "https://app.example.test:0/api/csp-reports",
      "https://app.example.test:65536/api/csp-reports",
      "https://app.example.test:443/api/csp-reports",
      "https://a;b.example.test/api/csp-reports",
      "https://a,b.example.test/api/csp-reports",
      "https://app.example.test/api/csp-reports;script-src",
      "https://app.example.test/api/csp-reports,script-src",
      ' https://app.example.test/api/csp-reports',
      "https://app.example.test/api/csp-\nreports",
      'https://app.example.test/api/csp-reports"',
      "https://-bad.example.test/api/csp-reports",
    ]) {
      assert.equal(reportUrlOf(value), null, String(value));
    }
  });
});

describe("CSP_REPORT_URL", () => {
  test("is read from the environment when the module loads", () => {
    // Wrong implementation killed: a module that throws at load whenever the variable is set.
    const read = (value) =>
      execFileSync(process.execPath, ["--input-type=module", "-e", `const m = await import(${JSON.stringify(new URL("../lib/csp.ts", import.meta.url).href)}); console.log(JSON.stringify(m.CSP_REPORT_URL));`], {
        env: { ...process.env, ZIF_CSP_REPORT_URL: value },
        encoding: "utf8",
      }).trim();
    assert.equal(read(GOOD), JSON.stringify(GOOD));
    assert.equal(read("https://reports.example.test/csp"), "null");
  });
});

describe("reportUrlFor", () => {
  test("fence: a page names the report URL only when the URL's host is the one the page was served on", () => {
    // Wrong implementation killed: a URL with a typo'd or another environment's host, which would send every
    // visitor's raw reports there cross-origin (report-uri needs no preflight).
    for (const host of ["app.example.test", "APP.Example.Test", "app.example.test:443"]) {
      assert.equal(reportUrlFor(host, GOOD), GOOD, host);
    }
    for (const host of [null, undefined, "", "dev.example.test", "app.example.test:8443", "app.example.test.evil", "a b", "app.example.test/x"]) {
      assert.equal(reportUrlFor(host, GOOD), null, String(host));
    }
    const port = "https://app.example.test:8443/api/csp-reports";
    assert.equal(reportUrlFor("app.example.test:8443", port), port);
    assert.equal(reportUrlFor("app.example.test", port), null);
    assert.equal(reportUrlFor("app.example.test", null), null);
  });
});

describe("the policy's reporting directives", () => {
  test("fence: with a report URL every policy names it, in both reporting forms", () => {
    // Wrong implementation killed: a policy that reports nowhere, so violations stay in each visitor's console.
    const policy = directives(cspFor("abc", null, null, GOOD));
    assert.equal(policy["report-uri"], GOOD);
    assert.equal(policy["report-to"], "csp");
    assert.deepEqual(reportingDirectives(GOOD), [`report-uri ${GOOD}`, "report-to csp"]);
  });

  test("unset: no reporting directive at all, rather than one pointing nowhere", () => {
    const policy = directives(cspFor("abc", null, null, null));
    assert.equal(policy["report-uri"], undefined);
    assert.equal(policy["report-to"], undefined);
    assert.deepEqual(reportingDirectives(null), []);
  });

  test("guard: browsers are never asked for a sample of the blocked script", () => {
    assert.doesNotMatch(cspFor("abc", "https://media.example.com", null, GOOD), /report-sample/);
  });
});

describe("cspStartupFields", () => {
  test("the web server's startup line says whether violations are reported, never where", () => {
    assert.deepEqual(cspStartupFields(undefined, false), { fields: { csp: "report-only", csp_reports: "off" }, refused: false });
    assert.deepEqual(cspStartupFields(GOOD, true), { fields: { csp: "enforce", csp_reports: "on" }, refused: false });
    // Set but refused: reports silently off unless the operator is told.
    const refused = cspStartupFields("https://Zzyzx-collector.example.test/csp", false);
    assert.deepEqual(refused, { fields: { csp: "report-only", csp_reports: "off" }, refused: true });
    assert.doesNotMatch(JSON.stringify(refused), /Zzyzx/i);
  });
});

describe("the endpoint the web pins", () => {
  test("guard: the API's contract has POST /api/csp-reports for both report types", () => {
    const api = JSON.parse(readFileSync(new URL("../../backend/openapi.json", import.meta.url), "utf8"));
    const operation = api.paths["/api/csp-reports"].post;
    assert.deepEqual(Object.keys(operation.requestBody.content).sort(), ["application/csp-report", "application/reports+json"]);
  });
});
