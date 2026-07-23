import assert from "node:assert/strict";
import test from "node:test";

import { consoleNavigation, consoleNavigationGroups } from "../src/lib/console-navigation.ts";
import { summaryValue } from "../src/lib/platform-summary.ts";

// Labels are i18n message keys now (resolved per-locale at render); the stable,
// locale-independent identity is `id`, so the surface contract is asserted on ids.
test("customer boxes expose the complete customer surface without control-plane links", () => {
  assert.deepEqual(
    consoleNavigation(false).map((item) => item.id),
    ["cockpit", "chat", "drive", "kpis", "ai-employees", "buchhaltung", "spaces", "privacy", "settings"],
  );
});

test("Mission Control exposes status, fleet control, user management, and account settings", () => {
  assert.deepEqual(
    consoleNavigation(true).map((item) => item.id),
    ["cockpit", "operator", "fleet", "users", "settings"],
  );
});

test("navigation groups preserve the authorized destination order", () => {
  assert.deepEqual(
    consoleNavigationGroups(false).map((group) => [group.id, group.items.map((item) => item.id)]),
    [
      ["monitor", ["cockpit"]],
      ["work", ["chat", "drive", "kpis", "ai-employees", "buchhaltung"]],
      ["manage", ["spaces", "privacy"]],
      ["account", ["settings"]],
    ],
  );
  assert.deepEqual(
    consoleNavigationGroups(true).flatMap((group) => group.items.map((item) => item.id)),
    ["cockpit", "operator", "fleet", "users", "settings"],
  );
});

test("platform summary does not turn loading or failed requests into zeroes", () => {
  assert.equal(summaryValue("loading", 0), "—");
  assert.equal(summaryValue("error", 0), "—");
  assert.equal(summaryValue("ready", 0), 0);
  assert.equal(summaryValue("ready", 5), 5);
});
