import type {
  CreateDriveFolderInput,
  CreateDriveUploadInput,
  DriveEntry,
  DriveFileEntry,
  DriveFolderEntry,
  DriveItemsResponse,
  DriveListInput,
  DriveUploadCompleteResult,
  DriveUploadSession,
  UpdateDriveFileInput,
  UpdateDriveFolderInput,
} from "./types";

const DRIVE_BASE = "/api/drive";

async function driveJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${DRIVE_BASE}${path}`, init);
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(apiErrorMessage(payload, `Drive request failed (${response.status}).`));
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export async function listDriveItems(input: DriveListInput): Promise<DriveItemsResponse> {
  const query = new URLSearchParams({
    account_id: input.root.account_id,
    space_id: input.root.space_id,
    view: input.view,
  });
  if (input.folderId) query.set("folder_id", input.folderId);
  if (input.query?.trim()) query.set("q", input.query.trim());
  if (input.cursor) query.set("cursor", input.cursor);
  const response = await driveJson<Partial<DriveItemsResponse>>(`/items?${query}`, { signal: input.signal });
  return {
    breadcrumbs: response.breadcrumbs ?? [],
    entries: response.entries ?? [],
    next_cursor: response.next_cursor ?? null,
  };
}

export async function createDriveFolder(input: CreateDriveFolderInput): Promise<DriveEntry> {
  const response = await driveJson<DriveEntry | { folder: DriveEntry }>("/folders", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      account_id: input.root.account_id,
      space_id: input.root.space_id,
      parent_folder_id: input.parentFolderId,
      name: input.name.trim(),
      idempotency_key: input.idempotencyKey,
      classification: input.policy.classification,
      location: input.policy.location,
      category: input.policy.category,
      index_for_ai: input.policy.indexForAi,
    }),
  });
  return "folder" in response ? response.folder : response;
}

export async function updateDriveFolder(input: UpdateDriveFolderInput): Promise<DriveFolderEntry> {
  const response = await driveJson<DriveFolderEntry | { folder: DriveFolderEntry }>(
    `/folders/${encodeURIComponent(input.folder.id)}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ...scopedMutationBody(input.folder, input.idempotencyKey),
        classification: input.policy.classification,
        location: input.policy.location,
        category: input.policy.category,
        index_for_ai: input.policy.indexForAi,
        confirm_audience_change: input.confirmAudienceChange,
      }),
    },
  );
  return "folder" in response ? response.folder : response;
}

export async function updateDriveFile(input: UpdateDriveFileInput): Promise<DriveFileEntry> {
  const response = await driveJson<DriveFileEntry | { file: DriveFileEntry }>(
    `/files/${encodeURIComponent(input.file.id)}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ...scopedMutationBody(input.file, input.idempotencyKey),
        folder_id: input.folderId,
        classification: input.policy.classification,
        location: input.policy.location,
        category: input.policy.category,
        index_for_ai: input.policy.indexForAi,
        confirm_audience_change: input.confirmAudienceChange,
      }),
    },
  );
  return "file" in response ? response.file : response;
}

export async function setDriveFileIndexing(
  file: DriveFileEntry,
  enabled: boolean,
): Promise<DriveFileEntry> {
  return fileMutation(`/files/${encodeURIComponent(file.id)}/indexing`, file, { enabled });
}

export async function approveDriveFile(file: DriveFileEntry): Promise<DriveFileEntry> {
  return fileMutation(`/files/${encodeURIComponent(file.id)}/approve`, file);
}

export async function permanentlyDeleteDriveFile(
  file: DriveFileEntry,
  reason: string,
): Promise<void> {
  await driveJson(`/files/${encodeURIComponent(file.id)}/permanent-delete`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      ...scopedMutationBody(file),
      reason: reason.trim(),
    }),
  });
}

export async function mutateDriveEntry(
  entry: DriveEntry,
  action: "trash" | "restore",
): Promise<void> {
  const collection = entry.kind === "folder" ? "folders" : "files";
  await driveJson(`/${collection}/${encodeURIComponent(entry.id)}/${action}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      ...scopedMutationBody(entry),
    }),
  });
}

export function driveDownloadHref(entry: DriveEntry): string {
  const query = new URLSearchParams({ account_id: entry.account_id, space_id: entry.space_id });
  return `${DRIVE_BASE}/files/${encodeURIComponent(entry.id)}/content?${query}`;
}

export async function createDriveUpload(input: CreateDriveUploadInput): Promise<DriveUploadSession> {
  const payload: Record<string, unknown> = {
    account_id: input.root.account_id,
    space_id: input.root.space_id,
    folder_id: input.folderId,
    name: input.file.name,
    size_bytes: input.file.size,
    idempotency_key: input.idempotencyKey,
  };
  payload.index_for_ai = input.indexForAi;
  const response = await driveJson<DriveUploadSession | { id: string } | { upload: { id: string; expires_at?: string } }>("/uploads", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const wireSession = "upload" in response ? response.upload : response;
  const uploadId = "upload_id" in wireSession ? wireSession.upload_id : wireSession.id;
  if (!uploadId) throw new Error("Drive did not return an upload session.");
  return { ...wireSession, upload_id: uploadId };
}

async function fileMutation(
  path: string,
  file: DriveFileEntry,
  fields: Record<string, unknown> = {},
): Promise<DriveFileEntry> {
  const response = await driveJson<DriveFileEntry | { file: DriveFileEntry }>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...scopedMutationBody(file), ...fields }),
  });
  return "file" in response ? response.file : response;
}

function scopedMutationBody(entry: DriveEntry, idempotencyKey = crypto.randomUUID()) {
  return {
    account_id: entry.account_id,
    space_id: entry.space_id,
    generation: entry.generation,
    idempotency_key: idempotencyKey,
  };
}

export function putDriveUploadContent(
  uploadId: string,
  file: File,
  onProgress: (progress: number) => void,
  signal?: AbortSignal,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    const abort = () => request.abort();
    request.open("PUT", `${DRIVE_BASE}/uploads/${encodeURIComponent(uploadId)}/content`);
    request.setRequestHeader("Content-Type", file.type || "application/octet-stream");
    request.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable && event.total > 0) {
        onProgress(Math.round((event.loaded / event.total) * 100));
      }
    });
    request.addEventListener("load", () => {
      signal?.removeEventListener("abort", abort);
      if (request.status >= 200 && request.status < 300) {
        onProgress(100);
        resolve();
        return;
      }
      reject(new Error(xhrErrorMessage(request)));
    });
    request.addEventListener("error", () => {
      signal?.removeEventListener("abort", abort);
      reject(new Error("The upload connection failed. Retry when the connection is stable."));
    });
    request.addEventListener("abort", () => {
      signal?.removeEventListener("abort", abort);
      reject(new DOMException("Upload canceled", "AbortError"));
    });
    if (signal?.aborted) {
      reject(new DOMException("Upload canceled", "AbortError"));
      return;
    }
    signal?.addEventListener("abort", abort, { once: true });
    request.send(file);
  });
}

export async function completeDriveUpload(
  uploadId: string,
  idempotencyKey: string,
): Promise<DriveUploadCompleteResult> {
  const response = await driveJson<DriveUploadCompleteResult | DriveFileEntry>(`/uploads/${encodeURIComponent(uploadId)}/complete`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ idempotency_key: idempotencyKey }),
  });
  return "file" in response ? response : { file: response };
}

function xhrErrorMessage(request: XMLHttpRequest): string {
  try {
    return apiErrorMessage(JSON.parse(request.responseText), `Upload failed (${request.status}).`);
  } catch {
    return `Upload failed (${request.status}).`;
  }
}

function apiErrorMessage(payload: unknown, fallback: string): string {
  if (payload && typeof payload === "object" && "detail" in payload) {
    const detail = (payload as { detail?: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      const messages = detail.flatMap((item) => (
        item && typeof item === "object" && "msg" in item && typeof item.msg === "string" ? [item.msg] : []
      ));
      if (messages.length) return messages.join(" ");
    }
  }
  return fallback;
}
