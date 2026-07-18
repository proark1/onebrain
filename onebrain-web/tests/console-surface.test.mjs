import assert from "node:assert/strict";
import test from "node:test";

import { consoleNavigation } from "../src/lib/console-navigation.ts";
import { summaryValue } from "../src/lib/platform-summary.ts";

test("customer boxes expose the complete customer surface without control-plane links", () => {
  assert.deepEqual(
    consoleNavigation(false).map((item) => item.label),
    ["Status", "Ask", "Knowledge", "KPIs", "AI Employees", "Apps", "Privacy", "Settings"],
  );
});

test("Mission Control exposes status, fleet control, user management, and account settings", () => {
  assert.deepEqual(
    consoleNavigation(true).map((item) => item.label),
    ["Status", "Control", "Fleet", "Users", "Settings"],
  );
});

test("platform summary does not turn loading or failed requests into zeroes", () => {
  assert.equal(summaryValue("loading", 0), "—");
  assert.equal(summaryValue("error", 0), "—");
  assert.equal(summaryValue("ready", 0), 0);
  assert.equal(summaryValue("ready", 5), 5);
});
