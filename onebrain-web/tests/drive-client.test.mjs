import assert from "node:assert/strict";
import test from "node:test";

import {
  DriveApiError,
  approveDriveFile,
  createDriveFolder,
  createDriveUpload,
  driveDownloadHref,
  listDriveItems,
  mutateDriveEntry,
  permanentlyDeleteDriveFile,
  rescanDriveFile,
  setDriveFileIndexing,
  updateDriveFile,
  updateDriveFolder,
} from "../src/features/drive/drive-client.ts";

const ROOT = { id: "root-1", account_id: "account/a", space_id: "space b", kind: "space", name: "Finance" };
const POLICY = { classification: "confidential", location: "munich", category: "finance", indexForAi: false };

function folder(overrides = {}) {
  return {
    kind: "folder",
    id: "folder_12345678",
    account_id: ROOT.account_id,
    space_id: ROOT.space_id,
    name: "Reports",
    parent_folder_id: "",
    generation: 4,
    classification: "internal",
    location: "global",
    category: "general",
    desired_indexed: true,
    index_status: "folder",
    updated_at: "",
    ...overrides,
  };
}

function file(overrides = {}) {
  return {
    kind: "file",
    id: "file_12345678",
    account_id: ROOT.account_id,
    space_id: ROOT.space_id,
    name: "plan.pdf",
    parent_folder_id: "",
    generation: 7,
    classification: "internal",
    location: "global",
    category: "general",
    updated_at: "",
    size_bytes: 12,
    media_type: "application/pdf",
    index_status: "not_indexed",
    desired_indexed: false,
    approval_status: "approved",
    malware_status: "clean",
    download_url: "/api/drive/files/file_12345678/content",
    ...overrides,
  };
}

test("Drive list and download requests always carry explicit account and space scope", async () => {
  const originalFetch = globalThis.fetch;
  let requested = "";
  globalThis.fetch = async (input) => {
    requested = String(input);
    return new Response(JSON.stringify({ entries: [], next_cursor: null }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };
  try {
    const result = await listDriveItems({ root: ROOT, folderId: "folder/1", view: "browse", query: "annual plan" });
    assert.deepEqual(result.breadcrumbs, []);
  } finally {
    globalThis.fetch = originalFetch;
  }

  const listUrl = new URL(requested, "https://onebrain.test");
  assert.equal(listUrl.pathname, "/api/drive/items");
  assert.equal(listUrl.searchParams.get("account_id"), ROOT.account_id);
  assert.equal(listUrl.searchParams.get("space_id"), ROOT.space_id);
  assert.equal(listUrl.searchParams.get("folder_id"), "folder/1");
  assert.equal(listUrl.searchParams.get("q"), "annual plan");

  const downloadUrl = new URL(driveDownloadHref(file({ id: "file/1", generation: 1, index_status: "indexed", desired_indexed: true })), "https://onebrain.test");
  assert.equal(downloadUrl.pathname, "/api/drive/files/file%2F1/content");
  assert.equal(downloadUrl.searchParams.get("account_id"), ROOT.account_id);
  assert.equal(downloadUrl.searchParams.get("space_id"), ROOT.space_id);
});

test("Drive mutations address the typed collection and send generation guards", async () => {
  const originalFetch = globalThis.fetch;
  let requested = "";
  let body = {};
  globalThis.fetch = async (input, init) => {
    requested = String(input);
    body = JSON.parse(String(init?.body));
    return new Response(null, { status: 204 });
  };
  try {
    await mutateDriveEntry(folder(), "trash");
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.equal(requested, "/api/drive/folders/folder_12345678/trash");
  assert.equal(body.account_id, ROOT.account_id);
  assert.equal(body.space_id, ROOT.space_id);
  assert.equal(body.generation, 4);
  assert.equal(typeof body.idempotency_key, "string");
});

test("Drive upload creation accepts the Core API upload wrapper", async () => {
  const originalFetch = globalThis.fetch;
  let body = {};
  globalThis.fetch = async (_input, init) => {
    body = JSON.parse(String(init?.body));
    return new Response(JSON.stringify({
      upload: { id: "upl_12345678", expires_at: "2026-07-19T12:00:00Z" },
    }), { status: 201, headers: { "Content-Type": "application/json" } });
  };
  try {
    const session = await createDriveUpload({
      root: ROOT,
      folderId: "",
      file: new File(["plan"], "plan.txt", { type: "text/plain" }),
      idempotencyKey: "upload-attempt-1",
      indexForAi: false,
      category: "",
    });
    assert.equal(session.upload_id, "upl_12345678");
    assert.equal(session.expires_at, "2026-07-19T12:00:00Z");
    assert.equal(body.folder_id, "");
    assert.equal(body.index_for_ai, false);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("Drive upload creation forwards the chosen department as the file category", async () => {
  const originalFetch = globalThis.fetch;
  const bodies = [];
  globalThis.fetch = async (_input, init) => {
    bodies.push(JSON.parse(String(init?.body)));
    return new Response(JSON.stringify({ upload: { id: "upl_12345678" } }), {
      status: 201,
      headers: { "Content-Type": "application/json" },
    });
  };
  try {
    // An explicit department (e.g. the Buchhaltung AccessGroup) rides through as `category`,
    // which is what routes the upload into the accounting capture pipeline.
    await createDriveUpload({
      root: ROOT,
      folderId: "",
      file: new File(["invoice"], "invoice.pdf", { type: "application/pdf" }),
      idempotencyKey: "upload-buchhaltung",
      indexForAi: true,
      category: "acg_space-b_buchhaltung",
    });
    // An empty category means "inherit the destination folder's default department"
    // and is still sent verbatim so the Core API can apply that default.
    await createDriveUpload({
      root: ROOT,
      folderId: "folder_12345678",
      file: new File(["memo"], "memo.txt", { type: "text/plain" }),
      idempotencyKey: "upload-inherit",
      indexForAi: false,
      category: "",
    });
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(bodies[0].category, "acg_space-b_buchhaltung");
  assert.equal(bodies[0].index_for_ai, true);
  assert.equal(bodies[1].category, "");
});

test("Drive folder creation and default updates send the complete filing policy", async () => {
  const originalFetch = globalThis.fetch;
  const requests = [];
  globalThis.fetch = async (input, init) => {
    requests.push({ url: String(input), method: init?.method, body: JSON.parse(String(init?.body)) });
    return new Response(JSON.stringify({ folder: folder({ ...POLICY, desired_indexed: false, generation: 5 }) }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };
  try {
    await createDriveFolder({ root: ROOT, parentFolderId: "parent-1", name: " Board ", idempotencyKey: "create-1", policy: POLICY });
    await updateDriveFolder({ folder: folder(), policy: POLICY, idempotencyKey: "update-1", confirmAudienceChange: true });
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(requests[0].url, "/api/drive/folders");
  assert.equal(requests[0].body.name, "Board");
  assert.equal(requests[0].body.classification, POLICY.classification);
  assert.equal(requests[0].body.location, POLICY.location);
  assert.equal(requests[0].body.category, POLICY.category);
  assert.equal(requests[0].body.index_for_ai, false);
  assert.equal(requests[1].url, "/api/drive/folders/folder_12345678");
  assert.equal(requests[1].method, "PATCH");
  assert.equal(requests[1].body.generation, 4);
  assert.equal(requests[1].body.confirm_audience_change, true);
});

test("Drive file filing, indexing, approval, and permanent deletion use scoped lifecycle routes", async () => {
  const originalFetch = globalThis.fetch;
  const requests = [];
  globalThis.fetch = async (input, init) => {
    requests.push({ url: String(input), method: init?.method, body: JSON.parse(String(init?.body)) });
    return new Response(JSON.stringify({ file: file({ generation: 8 }) }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };
  try {
    const entry = file();
    await updateDriveFile({ file: entry, folderId: "folder-2", policy: POLICY, idempotencyKey: "filing-1", confirmAudienceChange: false });
    await setDriveFileIndexing(entry, true);
    await approveDriveFile(entry);
    await rescanDriveFile(entry);
    await permanentlyDeleteDriveFile(entry, "Duplicate record");
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.deepEqual(requests.map((request) => request.url), [
    "/api/drive/files/file_12345678",
    "/api/drive/files/file_12345678/indexing",
    "/api/drive/files/file_12345678/approve",
    "/api/drive/files/file_12345678/rescan",
    "/api/drive/files/file_12345678/permanent-delete",
  ]);
  assert.equal(requests[0].method, "PATCH");
  assert.equal(requests[0].body.folder_id, "folder-2");
  assert.equal(requests[0].body.index_for_ai, false);
  assert.equal(requests[1].body.enabled, true);
  assert.equal(requests[2].body.generation, 7);
  assert.equal(requests[3].body.generation, 7);
  assert.equal(typeof requests[3].body.idempotency_key, "string");
  assert.equal(requests[4].body.reason, "Duplicate record");
  assert.ok(requests.every((request) => request.body.account_id === ROOT.account_id));
  assert.ok(requests.every((request) => request.body.space_id === ROOT.space_id));
});

test("Drive rescan reuses a bounded key for a stale-row retry and rotates on generation", async () => {
  const originalFetch = globalThis.fetch;
  const requests = [];
  globalThis.fetch = async (_input, init) => {
    requests.push(JSON.parse(String(init?.body)));
    return new Response(JSON.stringify({ file: file({ generation: 8 }) }), {
      status: 202,
      headers: { "Content-Type": "application/json" },
    });
  };
  try {
    const stale = file({ generation: 7 });
    await rescanDriveFile(stale);
    await rescanDriveFile(stale);
    await rescanDriveFile(file({ generation: 8 }));
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(requests[0].idempotency_key, requests[1].idempotency_key);
  assert.notEqual(requests[1].idempotency_key, requests[2].idempotency_key);
  assert.ok(requests.every((body) => body.idempotency_key.length <= 128));
});

test("Drive API errors preserve structured quarantine and capacity metadata", async () => {
  const originalFetch = globalThis.fetch;
  const cases = [
    {
      status: 423,
      code: "drive_revision_quarantined",
      message: "This file is unavailable until its security scan passes.",
      retryAfter: null,
    },
    {
      status: 503,
      code: "drive_quarantine_capacity_exhausted",
      message: "Secure upload capacity is temporarily full. Please retry later.",
      retryAfter: "60",
    },
  ];
  try {
    for (const expected of cases) {
      globalThis.fetch = async () => new Response(JSON.stringify({
        detail: { code: expected.code, message: expected.message },
      }), {
        status: expected.status,
        headers: {
          "Content-Type": "application/json",
          ...(expected.retryAfter ? { "Retry-After": expected.retryAfter } : {}),
        },
      });

      await assert.rejects(
        () => setDriveFileIndexing(file(), true),
        (error) => {
          assert.ok(error instanceof DriveApiError);
          assert.equal(error.message, expected.message);
          assert.equal(error.code, expected.code);
          assert.equal(error.status, expected.status);
          assert.equal(error.retryAfter, expected.retryAfter);
          return true;
        },
      );
    }
  } finally {
    globalThis.fetch = originalFetch;
  }
});
