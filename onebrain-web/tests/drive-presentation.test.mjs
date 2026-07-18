import assert from "node:assert/strict";
import test from "node:test";

import {
  canDownloadDriveEntry,
  driveAudienceSummary,
  driveStatusPresentation,
  formatDriveSize,
} from "../src/features/drive/drive-presentation.ts";

const FILE = {
  kind: "file",
  id: "file-1",
  account_id: "account-1",
  space_id: "space-1",
  name: "policy.pdf",
  parent_folder_id: "",
  generation: 1,
  classification: "confidential",
  location: "munich",
  category: "finance",
  updated_at: "2026-07-18T12:00:00Z",
  size_bytes: 2048,
  media_type: "application/pdf",
  index_status: "indexed",
  desired_indexed: true,
};

test("Drive AI states always expose text and an explanatory detail", () => {
  assert.deepEqual(driveStatusPresentation("indexed"), {
    label: "Indexed",
    detail: "AI can use this file for permitted audiences.",
    tone: "success",
  });
  assert.equal(driveStatusPresentation("awaiting_review").label, "Needs review");
  assert.equal(driveStatusPresentation("new_state").label, "New State");
});

test("Drive presentation formats metadata without weakening legacy download rules", () => {
  assert.equal(formatDriveSize(2048), "2.0 KB");
  assert.equal(driveAudienceSummary(FILE), "Confidential · Finance · Munich");
  assert.equal(canDownloadDriveEntry(FILE), true);
  assert.equal(canDownloadDriveEntry({ ...FILE, legacy: true }), false);
});
