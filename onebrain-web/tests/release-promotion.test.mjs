import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  canRetryDevelopmentRelease,
  developmentRetryRequiresRestoreAcknowledgement,
} from "../src/lib/release-promotion.ts";

test("restore-required development retries require both a review note and acknowledgement", () => {
  assert.equal(developmentRetryRequiresRestoreAcknowledgement("restore_required"), true);
  assert.equal(developmentRetryRequiresRestoreAcknowledgement(null), false);
  assert.equal(developmentRetryRequiresRestoreAcknowledgement(undefined), false);
  assert.equal(canRetryDevelopmentRelease("restore_required", "", false), false);
  assert.equal(canRetryDevelopmentRelease("restore_required", "Backup reviewed", false), false);
  assert.equal(canRetryDevelopmentRelease("restore_required", "", true), false);
  assert.equal(canRetryDevelopmentRelease("restore_required", "Backup reviewed", true), true);
  assert.equal(canRetryDevelopmentRelease("code_only", "", false), true);
});

test("retry client serializes the typed acknowledgement input", () => {
  const source = readFileSync(new URL("../src/lib/onebrain-client.ts", import.meta.url), "utf8");
  assert.match(source, /input: DevelopmentRetryInput/);
  assert.match(source, /releases\/\$\{encodeURIComponent\(version\)\}\/retry-dev/);
  assert.match(source, /body: JSON\.stringify\(input\)/);
});
