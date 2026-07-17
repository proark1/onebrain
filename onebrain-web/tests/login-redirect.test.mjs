import assert from "node:assert/strict";
import test from "node:test";

import { loginHref, safeLoginRedirect } from "../src/lib/login-redirect.ts";

test("allows normalized same-origin application paths", () => {
  assert.equal(safeLoginRedirect("/documents?tab=recent#top"), "/documents?tab=recent#top");
  assert.equal(safeLoginRedirect(["/spaces", "/ignored"]), "/spaces");
  assert.equal(loginHref("/documents?tab=recent"), "/login?next=%2Fdocuments%3Ftab%3Drecent");
});

test("rejects external, encoded, and login-loop redirect values", () => {
  for (const value of [
    "//evil.example",
    "/\\evil.example",
    "/%5C%5Cevil.example",
    "%2F%2Fevil.example",
    "https://evil.example/",
    "javascript:alert(1)",
    "/login",
    "/login?next=%2Ffleet",
    "/login%252Fcontinue",
  ]) {
    assert.equal(safeLoginRedirect(value), "/chat", value);
  }
});
