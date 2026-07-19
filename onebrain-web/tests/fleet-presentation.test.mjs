import assert from "node:assert/strict";
import test from "node:test";

import { describeFleetOverview, fleetHealthLabel, fleetHealthTone } from "../src/lib/fleet-presentation.ts";

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
