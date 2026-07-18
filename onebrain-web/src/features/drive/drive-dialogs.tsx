"use client";

import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import {
  defaultDrivePolicy,
  DriveFilingPolicyFields,
  drivePolicyWidens,
  entryDrivePolicy,
} from "./drive-policy-fields";
import type {
  DriveAudience,
  DriveFileEntry,
  DriveFilingPolicy,
  DriveFolderEntry,
  DrivePolicyMode,
} from "./types";
import styles from "./drive.module.css";

export type DriveFolderDestination = {
  id: string;
  name: string;
  policy?: DriveFilingPolicy;
};

type PolicyContext = {
  audience: DriveAudience;
  canIndex: boolean;
  policyMode: DrivePolicyMode;
};

export function CreateFolderDialog({
  audience,
  canIndex,
  open,
  policyMode,
  onClose,
  onCreate,
}: PolicyContext & {
  open: boolean;
  onClose: () => void;
  onCreate: (name: string, idempotencyKey: string, policy: DriveFilingPolicy) => Promise<void>;
}) {
  const dialogRef = useModal(open);
  const idempotencyKeyRef = useRef("");
  const titleId = useId();
  const [name, setName] = useState("");
  const [policy, setPolicy] = useState(() => defaultDrivePolicy(audience, canIndex));
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const cleanName = name.trim();
    if (!cleanName || busy) return;
    setBusy(true);
    setError("");
    try {
      idempotencyKeyRef.current ||= crypto.randomUUID();
      await onCreate(cleanName, idempotencyKeyRef.current, policy);
      closeDialog();
    } catch (err) {
      setError(messageFrom(err, "Could not create the folder."));
    } finally {
      setBusy(false);
    }
  }

  function closeDialog() {
    setName("");
    setError("");
    idempotencyKeyRef.current = "";
    onClose();
  }

  return (
    <dialog aria-labelledby={titleId} className={styles.dialog} ref={dialogRef} onCancel={(event) => { event.preventDefault(); closeDialog(); }}>
      <form onSubmit={(event) => void submit(event)}>
        <DialogHeader eyebrow="Drive filing template" id={titleId} title="New folder" copy="Files inherit these defaults when they are uploaded or moved here." />
        <label>
          <span>Folder name</span>
          <input autoFocus maxLength={180} value={name} onChange={(event) => setName(event.target.value)} />
        </label>
        <DriveFilingPolicyFields audience={audience} canIndex={canIndex} policy={policy} policyMode={policyMode} onChange={setPolicy} />
        <DialogError error={error} />
        <DialogFooter busy={busy} primary="Create folder" onCancel={closeDialog} disabled={!name.trim()} />
      </form>
    </dialog>
  );
}

export function UploadFilesDialog({
  canIndex,
  destinationName,
  open,
  policyMode,
  preselectedFiles,
  onClose,
  onUpload,
}: {
  canIndex: boolean;
  destinationName: string;
  open: boolean;
  policyMode: DrivePolicyMode;
  preselectedFiles: File[];
  onClose: () => void;
  onUpload: (files: File[], indexForAi: boolean) => void;
}) {
  const dialogRef = useModal(open);
  const titleId = useId();
  const inputId = useId();
  const [files, setFiles] = useState<File[]>(preselectedFiles);
  const [choice, setChoice] = useState<"index" | "store" | "">(canIndex ? "" : "store");

  function closeDialog() {
    setFiles([]);
    setChoice(canIndex ? "" : "store");
    onClose();
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!files.length || !choice) return;
    onUpload(files, choice === "index" && canIndex);
    closeDialog();
  }

  return (
    <dialog aria-labelledby={titleId} className={styles.dialog} ref={dialogRef} onCancel={(event) => { event.preventDefault(); closeDialog(); }}>
      <form onSubmit={submit}>
        <DialogHeader eyebrow="Upload" id={titleId} title="Add files" copy={`Choose how files entering ${destinationName} may be used.`} />
        <label className={styles.filePicker} htmlFor={inputId}>
          <span>{files.length ? `${files.length} file${files.length === 1 ? "" : "s"} selected` : "Choose files"}</span>
          <input id={inputId} multiple type="file" onChange={(event) => setFiles(Array.from(event.target.files ?? []))} />
          <small>{files.length ? summarizeFiles(files) : "Select one or more company files."}</small>
        </label>
        <fieldset className={styles.indexChoice}>
          <legend>AI use</legend>
          <label>
            <input checked={choice === "index"} disabled={!canIndex} name="index-choice" type="radio" onChange={() => setChoice("index")} />
            <span><strong>Index for AI</strong><small>Use in permitted AI answers after checks and approval.</small></span>
          </label>
          <label>
            <input checked={choice === "store"} name="index-choice" type="radio" onChange={() => setChoice("store")} />
            <span><strong>Store only</strong><small>The file remains available to people, but invisible to AI.</small></span>
          </label>
        </fieldset>
        {!canIndex ? <p className={styles.policyNotice}>{policyMode === "storage_only" ? "This deployment is in storage-only mode; indexing is unavailable." : "AI indexing is unavailable for your current Drive policy."}</p> : null}
        <DialogFooter busy={false} primary="Start upload" onCancel={closeDialog} disabled={!files.length || !choice} />
      </form>
    </dialog>
  );
}

export function FolderDefaultsDialog({
  audience,
  canConfirmWidening,
  canIndex,
  folder,
  open,
  policyMode,
  onClose,
  onSave,
}: PolicyContext & {
  canConfirmWidening: boolean;
  folder: DriveFolderEntry;
  open: boolean;
  onClose: () => void;
  onSave: (folder: DriveFolderEntry, policy: DriveFilingPolicy, idempotencyKey: string, confirm: boolean) => Promise<void>;
}) {
  const dialogRef = useModal(open);
  const titleId = useId();
  const [policy, setPolicy] = useState<DriveFilingPolicy>(() => effectiveDialogPolicy(folder, canIndex));
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const initial = entryDrivePolicy(folder);
  const widens = drivePolicyWidens(initial, policy, audience);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy || (widens && (!canConfirmWidening || !confirmed))) return;
    setBusy(true);
    setError("");
    try {
      await onSave(folder, policy, crypto.randomUUID(), widens && confirmed);
      onClose();
    } catch (err) {
      setError(messageFrom(err, "Could not update the folder defaults."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <dialog aria-labelledby={titleId} className={styles.dialog} ref={dialogRef} onCancel={(event) => { event.preventDefault(); onClose(); }}>
      <form onSubmit={(event) => void submit(event)}>
        <DialogHeader eyebrow="Manage defaults" id={titleId} title={folder.name} copy="New files inherit these labels. Existing files keep their current filing until changed." />
        <DriveFilingPolicyFields audience={audience} canIndex={canIndex} policy={policy} policyMode={policyMode} onChange={(next) => { setPolicy(next); setConfirmed(false); }} />
        <AudienceConfirmation canConfirm={canConfirmWidening} confirmed={confirmed} required={widens} onChange={setConfirmed} />
        <DialogError error={error} />
        <DialogFooter busy={busy} primary="Save defaults" onCancel={onClose} disabled={widens && (!canConfirmWidening || !confirmed)} />
      </form>
    </dialog>
  );
}

export function FileFilingDialog({
  audience,
  canConfirmWidening,
  canIndex,
  destinations,
  file,
  open,
  policyMode,
  onClose,
  onSave,
}: PolicyContext & {
  canConfirmWidening: boolean;
  destinations: DriveFolderDestination[];
  file: DriveFileEntry;
  open: boolean;
  onClose: () => void;
  onSave: (file: DriveFileEntry, folderId: string, policy: DriveFilingPolicy, idempotencyKey: string, confirm: boolean) => Promise<void>;
}) {
  const dialogRef = useModal(open);
  const titleId = useId();
  const [folderId, setFolderId] = useState("");
  const [policy, setPolicy] = useState<DriveFilingPolicy>(() => effectiveDialogPolicy(file, canIndex));
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const widens = drivePolicyWidens(entryDrivePolicy(file), policy, audience);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy || (widens && (!canConfirmWidening || !confirmed))) return;
    setBusy(true);
    setError("");
    try {
      await onSave(file, folderId, policy, crypto.randomUUID(), widens && confirmed);
      onClose();
    } catch (err) {
      setError(messageFrom(err, "Could not change the file filing."));
    } finally {
      setBusy(false);
    }
  }

  function chooseDestination(nextFolderId: string) {
    setFolderId(nextFolderId);
    const destination = destinations.find((item) => item.id === nextFolderId);
    if (destination?.policy) setPolicy(destination.policy);
    setConfirmed(false);
  }

  return (
    <dialog aria-labelledby={titleId} className={styles.dialog} ref={dialogRef} onCancel={(event) => { event.preventDefault(); onClose(); }}>
      <form onSubmit={(event) => void submit(event)}>
        <DialogHeader eyebrow="Move / change filing" id={titleId} title={file.name} copy="Moving can apply a folder's defaults and re-index the file for its new permitted audience." />
        <label>
          <span>Destination</span>
          <select value={folderId} onChange={(event) => chooseDestination(event.target.value)}>
            {destinations.map((item) => <option key={item.id || "root"} value={item.id}>{item.name}</option>)}
          </select>
        </label>
        <DriveFilingPolicyFields audience={audience} canIndex={canIndex} policy={policy} policyMode={policyMode} onChange={(next) => { setPolicy(next); setConfirmed(false); }} />
        <AudienceConfirmation canConfirm={canConfirmWidening} confirmed={confirmed} required={widens} onChange={setConfirmed} />
        <DialogError error={error} />
        <DialogFooter busy={busy} primary="Apply filing" onCancel={onClose} disabled={widens && (!canConfirmWidening || !confirmed)} />
      </form>
    </dialog>
  );
}

export function PermanentDeleteDialog({
  file,
  open,
  onClose,
  onDelete,
}: {
  file: DriveFileEntry;
  open: boolean;
  onClose: () => void;
  onDelete: (file: DriveFileEntry, reason: string) => Promise<void>;
}) {
  const dialogRef = useModal(open);
  const titleId = useId();
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (reason.trim().length < 3 || busy) return;
    setBusy(true);
    setError("");
    try {
      await onDelete(file, reason);
      onClose();
    } catch (err) {
      setError(messageFrom(err, "Could not permanently delete the file."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <dialog aria-labelledby={titleId} className={`${styles.dialog} ${styles.dangerDialog}`} ref={dialogRef} onCancel={(event) => { event.preventDefault(); onClose(); }}>
      <form onSubmit={(event) => void submit(event)}>
        <DialogHeader eyebrow="Administrator action" id={titleId} title={`Delete ${file.name}?`} copy="This removes the original, all revisions, and AI content. Legal holds can still block deletion. This cannot be undone." />
        <label>
          <span>Audit reason</span>
          <textarea autoFocus maxLength={500} rows={3} value={reason} onChange={(event) => setReason(event.target.value)} />
        </label>
        <DialogError error={error} />
        <DialogFooter busy={busy} primary="Delete permanently" onCancel={onClose} disabled={reason.trim().length < 3} danger />
      </form>
    </dialog>
  );
}

function AudienceConfirmation({
  canConfirm,
  confirmed,
  required,
  onChange,
}: {
  canConfirm: boolean;
  confirmed: boolean;
  required: boolean;
  onChange: (confirmed: boolean) => void;
}) {
  if (!required) return null;
  if (!canConfirm) return <p className={styles.policyWarning}>This change would widen who or what can use the item. An account administrator must make it.</p>;
  return (
    <label className={styles.confirmation}>
      <input checked={confirmed} type="checkbox" onChange={(event) => onChange(event.target.checked)} />
      <span>I understand this widens the audience or enables AI use.</span>
    </label>
  );
}

function DialogHeader({ eyebrow, id, title, copy }: { eyebrow: string; id: string; title: string; copy: string }) {
  return <header><p className={styles.eyebrow}>{eyebrow}</p><h2 id={id}>{title}</h2><p className={styles.dialogCopy}>{copy}</p></header>;
}

function DialogError({ error }: { error: string }) {
  return error ? <p className={styles.inlineError} role="alert">{error}</p> : null;
}

function DialogFooter({ busy, danger = false, disabled = false, primary, onCancel }: {
  busy: boolean;
  danger?: boolean;
  disabled?: boolean;
  primary: string;
  onCancel: () => void;
}) {
  return (
    <footer>
      <button className={styles.secondaryButton} disabled={busy} type="button" onClick={onCancel}>Cancel</button>
      <button className={danger ? styles.dangerButton : styles.primaryButton} disabled={disabled || busy} type="submit">{busy ? "Working…" : primary}</button>
    </footer>
  );
}

function useModal(open: boolean) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);
  return dialogRef;
}

function summarizeFiles(files: File[]): string {
  const names = files.slice(0, 2).map((file) => file.name).join(", ");
  return files.length > 2 ? `${names} and ${files.length - 2} more` : names;
}

function effectiveDialogPolicy(
  entry: Parameters<typeof entryDrivePolicy>[0],
  canIndex: boolean,
): DriveFilingPolicy {
  const policy = entryDrivePolicy(entry);
  return canIndex ? policy : { ...policy, indexForAi: false };
}

function messageFrom(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}
