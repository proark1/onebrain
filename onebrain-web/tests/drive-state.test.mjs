import assert from "node:assert/strict";
import test from "node:test";

import {
  createDriveBrowserState,
  driveBrowserReducer,
  driveUploadReducer,
} from "../src/features/drive/drive-state.ts";

const ROOT = { id: "root-1", account_id: "account-1", space_id: "space-1", kind: "space", name: "Finance" };

function bootstrap() {
  return {
    contract_version: 1,
    roots: [ROOT],
    selected_root: ROOT,
    breadcrumbs: [{ id: "folder-1", name: "Contracts" }],
    entries: [],
    next_cursor: null,
    counts: { review: 0, trash: 0, legacy: 0 },
    capabilities: {
      can_upload: true,
      can_create_folder: true,
      can_review: false,
      can_manage_labels: false,
      can_index: true,
      can_permanently_delete: false,
      policy_mode: "storage_and_indexing",
    },
    upload: { max_file_bytes: 1024 },
    audience: { classifications: ["internal"], locations: ["global"], departments: [{ id: "general", name: "Everyone" }] },
  };
}

test("Drive browser state scopes folder navigation to the selected root", () => {
  const initial = createDriveBrowserState(bootstrap());
  assert.equal(initial.folderId, "folder-1");

  const trash = driveBrowserReducer(initial, { type: "select_view", view: "trash" });
  assert.equal(trash.view, "trash");
  assert.equal(trash.folderId, "");
  assert.deepEqual(trash.entries, []);

  const folder = driveBrowserReducer(trash, { type: "select_folder", folderId: "folder-2" });
  assert.equal(folder.view, "browse");
  assert.equal(folder.folderId, "folder-2");
});

test("Drive upload reducer prevents progress from moving backwards and supports retry", () => {
  const record = {
    id: "upload-1",
    attempt: 0,
    name: "plan.pdf",
    sizeBytes: 50,
    accountId: "account-1",
    spaceId: "space-1",
    folderId: "",
    indexForAi: true,
    progress: 0,
    status: "queued",
    error: "",
    retryable: true,
  };
  let state = driveUploadReducer([], { type: "enqueue", records: [record] });
  state = driveUploadReducer(state, { type: "progress", id: record.id, progress: 75 });
  state = driveUploadReducer(state, { type: "progress", id: record.id, progress: 40 });
  assert.equal(state[0].progress, 75);

  state = driveUploadReducer(state, { type: "failed", id: record.id, message: "Connection lost" });
  assert.equal(state[0].status, "failed");
  state = driveUploadReducer(state, { type: "retry", id: record.id });
  assert.equal(state[0].status, "queued");
  assert.equal(state[0].progress, 0);
  assert.equal(state[0].error, "");
  assert.equal(state[0].attempt, 1);
});

test("Drive browser state replaces a generation-guarded entry after a lifecycle mutation", () => {
  const entry = {
    kind: "file",
    id: "file-1",
    account_id: "account-1",
    space_id: "space-1",
    parent_folder_id: "",
    generation: 1,
    name: "plan.pdf",
    classification: "internal",
    location: "global",
    category: "general",
    updated_at: "",
    size_bytes: 20,
    media_type: "application/pdf",
    desired_indexed: false,
    index_status: "not_indexed",
  };
  const initial = { ...createDriveBrowserState(bootstrap()), entries: [entry] };
  const updated = { ...entry, generation: 2, desired_indexed: true, index_status: "queued" };
  const state = driveBrowserReducer(initial, { type: "replace_entry", entry: updated });
  assert.deepEqual(state.entries, [updated]);
});
