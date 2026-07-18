"use client";

import { useCallback, useEffect, useReducer, useRef } from "react";
import {
  completeDriveUpload,
  createDriveUpload,
  putDriveUploadContent,
} from "./drive-client";
import { DriveIcon } from "./drive-icons";
import { driveSecurityPresentation, formatDriveSize } from "./drive-presentation";
import {
  driveUploadReducer,
  type DriveUploadRecord,
  type DriveUploadStatus,
} from "./drive-state";
import type { DriveFileEntry, DriveRoot } from "./types";
import styles from "./drive.module.css";

type PendingFile = {
  file: File;
  folderId: string;
  indexForAi: boolean;
  root: DriveRoot;
};

const ACTIVE_UPLOADS = new Set<DriveUploadStatus>(["creating", "uploading", "completing"]);
const MAX_CONCURRENT_UPLOADS = 3;

export function useDriveUploads({
  maxFileBytes,
  onComplete,
}: {
  maxFileBytes: number;
  onComplete: (file: DriveFileEntry) => void;
}) {
  const [uploads, dispatch] = useReducer(driveUploadReducer, []);
  const filesRef = useRef(new Map<string, PendingFile>());
  const runningRef = useRef(new Set<string>());
  const abortRef = useRef(new Map<string, AbortController>());
  const onCompleteRef = useRef(onComplete);

  useEffect(() => {
    onCompleteRef.current = onComplete;
  }, [onComplete]);

  useEffect(() => () => {
    for (const controller of abortRef.current.values()) controller.abort();
  }, []);

  const runUpload = useCallback(async (record: DriveUploadRecord) => {
    const pending = filesRef.current.get(record.id);
    if (!pending) {
      dispatch({ type: "failed", id: record.id, message: "The selected file is no longer available. Choose it again." });
      return;
    }
    const controller = new AbortController();
    abortRef.current.set(record.id, controller);
    try {
      dispatch({ type: "status", id: record.id, status: "creating" });
      const session = await createDriveUpload({
        root: pending.root,
        folderId: pending.folderId,
        file: pending.file,
        idempotencyKey: `${record.id}:${record.attempt}:create`,
        indexForAi: pending.indexForAi,
      });
      dispatch({ type: "status", id: record.id, status: "uploading" });
      await putDriveUploadContent(
        session.upload_id,
        pending.file,
        (progress) => dispatch({ type: "progress", id: record.id, progress }),
        controller.signal,
      );
      dispatch({ type: "status", id: record.id, status: "completing" });
      const result = await completeDriveUpload(session.upload_id, `${record.id}:${record.attempt}:complete`);
      dispatch({
        type: "completed",
        id: record.id,
        fileId: result.file.id,
        malwareStatus: result.file.malware_status,
      });
      onCompleteRef.current(result.file);
    } catch (err) {
      if (controller.signal.aborted) {
        dispatch({ type: "cancel", id: record.id });
      } else {
        dispatch({
          type: "failed",
          id: record.id,
          message: err instanceof Error ? err.message : "Upload failed. Retry when ready.",
        });
      }
    } finally {
      runningRef.current.delete(record.id);
      abortRef.current.delete(record.id);
    }
  }, []);

  useEffect(() => {
    const capacity = MAX_CONCURRENT_UPLOADS - runningRef.current.size;
    if (capacity <= 0) return;
    const next = uploads
      .filter((upload) => upload.status === "queued" && !runningRef.current.has(upload.id))
      .slice(0, capacity);
    for (const upload of next) {
      runningRef.current.add(upload.id);
      void runUpload(upload);
    }
  }, [runUpload, uploads]);

  const enqueue = useCallback((files: File[], root: DriveRoot | null, folderId: string, indexForAi: boolean) => {
    if (!root || files.length === 0) return;
    const records = files.map<DriveUploadRecord>((file) => {
      const id = crypto.randomUUID();
      filesRef.current.set(id, { file, folderId, indexForAi, root });
      const tooLarge = maxFileBytes > 0 && file.size > maxFileBytes;
      return {
        id,
        attempt: 0,
        name: file.name,
        sizeBytes: file.size,
        accountId: root.account_id,
        spaceId: root.space_id,
        folderId,
        indexForAi,
        progress: 0,
        status: tooLarge ? "failed" : "queued",
        error: tooLarge ? `${file.name} is larger than the ${formatDriveSize(maxFileBytes)} upload limit.` : "",
        retryable: !tooLarge,
      };
    });
    dispatch({ type: "enqueue", records });
  }, [maxFileBytes]);

  const cancel = useCallback((id: string) => {
    abortRef.current.get(id)?.abort();
    dispatch({ type: "cancel", id });
  }, []);

  const retry = useCallback((id: string) => {
    if (!runningRef.current.has(id)) dispatch({ type: "retry", id });
  }, []);

  const dismiss = useCallback((id: string) => {
    if (runningRef.current.has(id)) return;
    filesRef.current.delete(id);
    dispatch({ type: "dismiss", id });
  }, []);

  const syncSecurity = useCallback((files: DriveFileEntry[]) => {
    dispatch({
      type: "sync_security",
      files: files.map((file) => ({ id: file.id, malwareStatus: file.malware_status })),
    });
  }, []);

  return { cancel, dismiss, enqueue, retry, syncSecurity, uploads };
}

export function DriveUploadTray({
  uploads,
  onCancel,
  onDismiss,
  onRetry,
}: {
  uploads: DriveUploadRecord[];
  onCancel: (id: string) => void;
  onDismiss: (id: string) => void;
  onRetry: (id: string) => void;
}) {
  if (uploads.length === 0) return null;
  const activeCount = uploads.filter((upload) => ACTIVE_UPLOADS.has(upload.status) || upload.status === "queued").length;
  const scanCount = uploads.filter((upload) => (
    upload.status === "stored"
    && (upload.malwareStatus === "pending" || upload.malwareStatus === "scanning")
  )).length;
  const heading = activeCount
    ? `Uploading ${activeCount}`
    : scanCount
      ? `Security scanning ${scanCount}`
      : "Uploads";
  return (
    <section className={styles.uploadTray} aria-label="Uploads" aria-live="polite">
      <header>
        <div>
          <DriveIcon name="upload" />
          <strong>{heading}</strong>
        </div>
        <span>{uploads.length} file{uploads.length === 1 ? "" : "s"}</span>
      </header>
      <div className={styles.uploadList}>
        {uploads.map((upload) => (
          <article className={styles.uploadRow} key={upload.id}>
            <div className={styles.uploadCopy}>
              <strong>{upload.name}</strong>
              <span>{uploadStatusLabel(upload)}</span>
            </div>
            <div className={styles.uploadProgress} aria-label={`${upload.progress}% uploaded`} role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={upload.progress}>
              <span style={{ width: `${upload.progress}%` }} />
            </div>
            {upload.error ? <p role="alert">{upload.error}</p> : null}
            <div className={styles.uploadActions}>
              {upload.status === "failed" && upload.retryable ? <button type="button" onClick={() => onRetry(upload.id)}>Retry</button> : null}
              {upload.status === "queued" || ACTIVE_UPLOADS.has(upload.status) ? <button type="button" onClick={() => onCancel(upload.id)}>Cancel</button> : null}
              {["stored", "failed", "canceled"].includes(upload.status) ? <button type="button" onClick={() => onDismiss(upload.id)}>Dismiss</button> : null}
            </div>
          </article>
        ))}
      </div>
    </section>
  );
}

function uploadStatusLabel(upload: DriveUploadRecord): string {
  const labels: Record<DriveUploadStatus, string> = {
    queued: "Waiting",
    creating: "Preparing upload",
    uploading: `${upload.progress}% uploaded`,
    completing: "Storing and checking",
    stored: driveSecurityPresentation(upload.malwareStatus).label,
    failed: "Upload needs attention",
    canceled: "Canceled",
  };
  return `${labels[upload.status]} · ${formatDriveSize(upload.sizeBytes)}`;
}
