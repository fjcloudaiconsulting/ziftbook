// ZIF-146: the only way API text becomes an href on the booking, manage and account pages.
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { safeLink } from "../lib/safe-link.ts";

describe("safeLink", () => {
  test("an https link and a mail to one plain address pass unchanged", () => {
    for (const url of ["https://salon.example.com/about", "mailto:hello@salon.example.com"]) {
      assert.equal(safeLink(url), url);
    }
  });

  test("fence: anything that could run script, carry data, or reach past one address renders no link", () => {
    // Wrong implementation killed: a check on the prefix alone ("starts with http"), or none.
    for (const url of [
      "javascript:alert(1)",
      " javascript:alert(1)",
      "JaVaScRiPt:alert(1)",
      "data:text/html,<script>alert(1)</script>",
      "vbscript:msgbox",
      "http://salon.example.com",
      "httpx://salon.example.com",
      "mailto:a@example.com?bcc=b@example.com",
      "mailto:a@example.com,b@example.com",
      "mailto:a@example.com&body=x",
      "//evil.example",
      "/relative/path",
      "",
      null,
      undefined,
    ]) {
      assert.equal(safeLink(url), undefined, String(url));
    }
  });
});
