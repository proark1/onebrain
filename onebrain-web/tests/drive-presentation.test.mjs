import assert from "node:assert/strict";
import test from "node:test";

import {
  canDownloadDriveEntry,
  canRescanDriveEntry,
  driveAudienceSummary,
  driveSecurityPresentation,
  driveStatusPresentation,
  formatDriveSize,
  shouldPollDriveSecurity,
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
  malware_status: "clean",
  download_url: "/api/drive/files/file-1/content",
};

test("Drive AI states always expose text and an explanatory detail", () => {
  assert.deepEqual(driveStatusPresentation("indexed"), {
    label: "Indexed",
    detail: "AI can use this file for permitted audiences.",
    tone: "success",
  });
  assert.equal(driveStatusPresentation("awaiting_review").label, "Needs review");
  assert.equal(driveStatusPresentation("awaiting_scan").label, "Waiting for security");
  assert.equal(driveStatusPresentation("new_state").label, "New State");
});

test("Drive security states stay distinct from AI status and never claim a file is safe", () => {
  assert.deepEqual(driveSecurityPresentation("clean"), {
    label: "No known malware found",
    detail: "The current file revision passed the required malware scan.",
    tone: "success",
  });
  assert.equal(driveSecurityPresentation("infected").label, "Threat blocked");
  assert.equal(driveSecurityPresentation("scan_error").label, "Scan unavailable — retrying");
  assert.equal(driveSecurityPresentation(undefined).label, "Security check required");
});

test("Drive presentation formats metadata without weakening legacy download rules", () => {
  assert.equal(formatDriveSize(2048), "2.0 KB");
  assert.equal(driveAudienceSummary(FILE), "Confidential · Finance · Munich");
  assert.equal(canDownloadDriveEntry(FILE), true);
  assert.equal(canDownloadDriveEntry({ ...FILE, legacy: true }), false);
  assert.equal(canDownloadDriveEntry({ ...FILE, malware_status: "pending", download_url: undefined }), false);
  assert.equal(canDownloadDriveEntry({ ...FILE, malware_status: undefined }), false);
  assert.equal(canDownloadDriveEntry({ ...FILE, download_url: undefined }), false);
});

test("Drive security helpers poll only active scans and offer rescan only for terminal non-clean results", () => {
  assert.equal(shouldPollDriveSecurity({ ...FILE, malware_status: "pending" }), true);
  assert.equal(shouldPollDriveSecurity({ ...FILE, malware_status: "scanning" }), true);
  assert.equal(shouldPollDriveSecurity({ ...FILE, malware_status: "clean" }), false);
  assert.equal(shouldPollDriveSecurity({ ...FILE, malware_status: "infected" }), false);
  assert.equal(canRescanDriveEntry({ ...FILE, malware_status: "infected" }), true);
  assert.equal(canRescanDriveEntry({ ...FILE, malware_status: "scan_error" }), true);
  assert.equal(canRescanDriveEntry({ ...FILE, malware_status: "rescan_required" }), true);
  assert.equal(canRescanDriveEntry({ ...FILE, malware_status: "pending" }), false);
});
