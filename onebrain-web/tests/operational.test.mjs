import assert from "node:assert/strict";
import test from "node:test";
import { describeOperationalStatus, formatOperationalTimestamp } from "../src/lib/operational.ts";

test("formatOperationalTimestamp renders a local date, time, and relative age", () => {
  const timestamp = formatOperationalTimestamp("2026-07-17T12:30:00.000Z", {
    locale: "en-GB",
    now: new Date("2026-07-17T13:35:00.000Z"),
    timeZone: "UTC",
  });

  assert.equal(timestamp.isMissing, false);
  assert.equal(timestamp.dateTime, "2026-07-17T12:30:00.000Z");
  assert.match(timestamp.local, /17 Jul 2026/);
  assert.match(timestamp.local, /12:30/);
  assert.equal(timestamp.relative, "1 hour ago");
});

test("formatOperationalTimestamp makes invalid and absent signals explicit", () => {
  assert.deepEqual(formatOperationalTimestamp(null), {
    dateTime: "",
    isMissing: true,
    local: "No signal received yet",
    relative: "Not yet reported",
  });
  assert.deepEqual(formatOperationalTimestamp("not-a-date"), {
    dateTime: "",
    isMissing: true,
    local: "No signal received yet",
    relative: "Not yet reported",
  });
});

test("describeOperationalStatus maps raw states to clear operator language", () => {
  assert.deepEqual(describeOperationalStatus("healthy"), {
    condition: "Healthy",
    explanation: "The latest report shows this service is operating normally.",
    nextAction: "No action needed. Continue monitoring.",
    tone: "success",
  });
  assert.deepEqual(describeOperationalStatus("updating"), {
    condition: "Updating",
    explanation: "A change is in progress and the latest report has not completed yet.",
    nextAction: "Wait for the next report before taking further action.",
    tone: "running",
  });
  assert.deepEqual(describeOperationalStatus("failed"), {
    condition: "Needs attention",
    explanation: "The latest report indicates a failure that needs review.",
    nextAction: "Open the details, review the failure, and decide the recovery action.",
    tone: "danger",
  });
  assert.equal(describeOperationalStatus("rollout_failed").tone, "danger");
  assert.equal(describeOperationalStatus("succeeded").condition, "Healthy");
  assert.deepEqual(describeOperationalStatus("not_deployed"), {
    condition: "Pending",
    explanation: "This customer has not been deployed yet.",
    nextAction: "Choose an initial version in Control and start the first deployment.",
    tone: "warning",
  });
  assert.deepEqual(describeOperationalStatus("paused"), {
    condition: "Needs attention",
    explanation: "This work is paused and will not continue until an operator decides what to do.",
    nextAction: "Open the details and decide whether to resume, retry, or leave it stopped.",
    tone: "warning",
  });
  assert.deepEqual(describeOperationalStatus("pending"), {
    condition: "Pending",
    explanation: "This work is waiting for a report or the next execution step.",
    nextAction: "Check that the responsible service is connected and reporting.",
    tone: "warning",
  });
  assert.deepEqual(describeOperationalStatus(""), {
    condition: "Not yet reported",
    explanation: "No operational signal has been received yet.",
    nextAction: "Check the connection and wait for the first report.",
    tone: "neutral",
  });
});
