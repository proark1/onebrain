import assert from "node:assert/strict";
import test from "node:test";

import { absoluteHostUrl, describeFleetOverview, fleetHealthLabel, fleetHealthTone } from "../src/lib/fleet-presentation.ts";

test("a stored host becomes an absolute url instead of a relative path", () => {
  // The bug this guards: a schemeless href navigates inside Mission Control.
  assert.equal(absoluteHostUrl("box.example.com"), "https://box.example.com");
  assert.equal(absoluteHostUrl("https://box.example.com/"), "https://box.example.com");
  assert.equal(absoluteHostUrl("http://box.example.com"), "http://box.example.com");
});

test("a dns-less box links over http because no certificate exists for an ip", () => {
  assert.equal(absoluteHostUrl("203.0.113.9"), "http://203.0.113.9");
});

test("nothing linkable yields an empty string, not a dead link", () => {
  assert.equal(absoluteHostUrl(""), "");
  assert.equal(absoluteHostUrl("   "), "");
});

test("fleet health presentation makes missing signals explicit", () => {
  assert.equal(fleetHealthLabel(true), "Healthy");
  assert.equal(fleetHealthTone(true), "success");
  assert.equal(fleetHealthLabel(false), "Needs attention");
  assert.equal(fleetHealthTone(false), "danger");
  assert.equal(fleetHealthLabel(null), "No signal");
  assert.equal(fleetHealthTone(null), "neutral");
});

test("fleet overview leads with the decision instead of raw counts", () => {
  const healthy = describeFleetOverview({ generated_at: "", deployments: [], total: 2, healthy: 2, with_open_alerts: 0 });
  assert.equal(healthy.condition, "All deployments are healthy");
  assert.equal(healthy.tone, "success");

  const alerting = describeFleetOverview({ generated_at: "", deployments: [], total: 2, healthy: 1, with_open_alerts: 1 });
  assert.equal(alerting.condition, "1 deployment needs attention");
  assert.equal(alerting.tone, "danger");

  const missing = describeFleetOverview({ generated_at: "", deployments: [], total: 2, healthy: 1, with_open_alerts: 0 });
  assert.equal(missing.condition, "1 deployment signal needs review");
  assert.equal(missing.tone, "warning");
});
