import assert from "node:assert/strict";
import test from "node:test";

import {
  parseUserManagementState,
  userManagementState,
} from "../src/lib/user-management-pending.ts";

test("pending user-management state stores only resumable identifiers", () => {
  const value = userManagementState("dep-1", true, "umj_123");
  assert.deepEqual(value, {
    version: 1,
    deployment_id: "dep-1",
    include_deleted: true,
    job_id: "umj_123",
  });
  assert.deepEqual(parseUserManagementState(JSON.stringify(value)), value);
});

test("pending user-management state rejects malformed and unversioned records", () => {
  assert.equal(parseUserManagementState(null), null);
  assert.equal(parseUserManagementState("not-json"), null);
  assert.equal(parseUserManagementState(JSON.stringify({ version: 2, deployment_id: "dep-1", include_deleted: false })), null);
  assert.equal(parseUserManagementState(JSON.stringify({ version: 1, deployment_id: "dep-1", include_deleted: false, job_id: "other" })), null);
});
