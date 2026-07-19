import assert from "node:assert/strict";
import test from "node:test";

import { consoleNavigation, consoleNavigationGroups } from "../src/lib/console-navigation.ts";
import { summaryValue } from "../src/lib/platform-summary.ts";

test("customer boxes expose the complete customer surface without control-plane links", () => {
  assert.deepEqual(
    consoleNavigation(false).map((item) => item.label),
    ["Status", "Ask", "Drive", "KPIs", "AI Employees", "Apps", "Privacy", "Settings"],
  );
});

test("Mission Control exposes status, fleet control, user management, and account settings", () => {
  assert.deepEqual(
    consoleNavigation(true).map((item) => item.label),
    ["Status", "Control", "Fleet", "Users", "Settings"],
  );
});

test("navigation groups preserve the authorized destination order", () => {
  assert.deepEqual(
    consoleNavigationGroups(false).map((group) => [group.label, group.items.map((item) => item.label)]),
    [
      ["Monitor", ["Status"]],
      ["Work", ["Ask", "Drive", "KPIs", "AI Employees"]],
      ["Manage", ["Apps", "Privacy"]],
      ["Account", ["Settings"]],
    ],
  );
  assert.deepEqual(
    consoleNavigationGroups(true).flatMap((group) => group.items.map((item) => item.label)),
    ["Status", "Control", "Fleet", "Users", "Settings"],
  );
});

test("platform summary does not turn loading or failed requests into zeroes", () => {
  assert.equal(summaryValue("loading", 0), "—");
  assert.equal(summaryValue("error", 0), "—");
  assert.equal(summaryValue("ready", 0), 0);
  assert.equal(summaryValue("ready", 5), 5);
});
