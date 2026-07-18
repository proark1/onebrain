import type { DragEvent } from "react";
import { DriveAiStatus, DriveSecurityStatus } from "./drive-status";
import { DriveIcon } from "./drive-icons";
import {
  canDownloadDriveEntry,
  canRescanDriveEntry,
  driveAudienceSummary,
  driveFileKind,
  formatDriveDate,
  formatDriveSize,
} from "./drive-presentation";
import type {
  DriveCapabilities,
  DriveEntry,
  DriveFileEntry,
  DriveFolderEntry,
  DriveView,
} from "./types";
import styles from "./drive.module.css";

type DriveEntryListProps = {
  actionId: string;
  capabilities: DriveCapabilities;
  entries: DriveEntry[];
  loading: boolean;
  nextCursor: string | null;
  view: DriveView;
  onDropFiles: (files: File[], folderId: string) => void;
  onApprove: (file: DriveFileEntry) => void;
  onLoadMore: () => void;
  onManageFile: (file: DriveFileEntry) => void;
  onManageFolder: (folder: DriveFolderEntry) => void;
  onOpenFolder: (folderId: string) => void;
  onPermanentDelete: (file: DriveFileEntry) => void;
  onRescan: (file: DriveFileEntry) => void;
  onRestore: (entry: DriveEntry) => void;
  onToggleIndexing: (file: DriveFileEntry) => void;
  onTrash: (entry: DriveEntry) => void;
};

export function DriveEntryList({
  actionId,
  capabilities,
  entries,
  loading,
  nextCursor,
  view,
  onDropFiles,
  onApprove,
  onLoadMore,
  onManageFile,
  onManageFolder,
  onOpenFolder,
  onPermanentDelete,
  onRescan,
  onRestore,
  onToggleIndexing,
  onTrash,
}: DriveEntryListProps) {
  return (
    <section className={styles.entryPanel} aria-busy={loading} aria-label="Files and folders">
      <div className={styles.listHeader} aria-hidden="true">
        <span>Name</span><span>Access</span><span>Security</span><span>AI</span><span>Updated</span><span>Actions</span>
      </div>
      <div className={styles.entryList}>
        {entries.map((entry) => (
          <DriveEntryRow
            actionId={actionId}
            capabilities={capabilities}
            entry={entry}
            key={`${entry.kind}:${entry.id}`}
            view={view}
            onDropFiles={onDropFiles}
            onApprove={onApprove}
            onManageFile={onManageFile}
            onManageFolder={onManageFolder}
            onOpenFolder={onOpenFolder}
            onPermanentDelete={onPermanentDelete}
            onRescan={onRescan}
            onRestore={onRestore}
            onToggleIndexing={onToggleIndexing}
            onTrash={onTrash}
          />
        ))}
        {!entries.length && !loading ? (
          <div className={styles.emptyState}>
            <DriveIcon name={view === "trash" ? "trash" : view === "legacy" ? "legacy" : "folder"} size={28} />
            <h2>{emptyTitle(view)}</h2>
            <p>{emptyCopy(view)}</p>
          </div>
        ) : null}
        {loading ? <div className={styles.loadingState} role="status">Loading files…</div> : null}
      </div>
      {nextCursor ? <button className={styles.loadMore} disabled={loading} type="button" onClick={onLoadMore}>Load more</button> : null}
    </section>
  );
}

function DriveEntryRow({
  actionId,
  capabilities,
  entry,
  view,
  onDropFiles,
  onApprove,
  onManageFile,
  onManageFolder,
  onOpenFolder,
  onPermanentDelete,
  onRescan,
  onRestore,
  onToggleIndexing,
  onTrash,
}: {
  actionId: string;
  capabilities: DriveCapabilities;
  entry: DriveEntry;
  view: DriveView;
  onDropFiles: (files: File[], folderId: string) => void;
  onApprove: (file: DriveFileEntry) => void;
  onManageFile: (file: DriveFileEntry) => void;
  onManageFolder: (folder: DriveFolderEntry) => void;
  onOpenFolder: (folderId: string) => void;
  onPermanentDelete: (file: DriveFileEntry) => void;
  onRescan: (file: DriveFileEntry) => void;
  onRestore: (entry: DriveEntry) => void;
  onToggleIndexing: (file: DriveFileEntry) => void;
  onTrash: (entry: DriveEntry) => void;
}) {
  const busy = actionId === entry.id;
  const downloadUrl = canDownloadDriveEntry(entry) ? entry.download_url : "";
  const download = Boolean(downloadUrl);
  function drop(event: DragEvent<HTMLElement>) {
    if (entry.kind !== "folder" || event.dataTransfer.files.length === 0) return;
    event.preventDefault();
    event.stopPropagation();
    onDropFiles(Array.from(event.dataTransfer.files), entry.id);
  }
  return (
    <article
      className={styles.entryRow}
      onDragOver={(event) => { if (entry.kind === "folder" && event.dataTransfer.types.includes("Files")) event.preventDefault(); }}
      onDrop={drop}
    >
      <div className={styles.entryName}>
        <span className={entry.kind === "folder" ? styles.folderMark : styles.fileMark} aria-hidden="true">
          {entry.kind === "folder" ? <DriveIcon name="folder" /> : driveFileKind(entry.name, entry.media_type)}
        </span>
        <div>
          {entry.kind === "folder" ? (
            <button type="button" onClick={() => onOpenFolder(entry.id)}>{entry.name}</button>
          ) : download ? (
            <a href={downloadUrl}>{entry.name}</a>
          ) : <strong>{entry.name}</strong>}
          <small>{entry.kind === "file" ? (entry.legacy ? "Original unavailable" : formatDriveSize(entry.size_bytes)) : `${entry.child_count ?? 0} items`}</small>
        </div>
      </div>
      <span className={styles.audience} data-label="Access">{driveAudienceSummary(entry)}</span>
      <div className={styles.securityColumn} data-label="Security">
        {entry.kind === "file" ? <DriveSecurityStatus status={entry.malware_status} /> : <span className={styles.folderPolicy}>Scanned on upload</span>}
      </div>
      <div className={styles.aiColumn} data-label="AI">
        {entry.kind === "file" ? <DriveAiStatus status={entry.index_status} /> : <span className={styles.folderPolicy}>Folder defaults</span>}
      </div>
      <span className={styles.updated} data-label="Updated">{formatDriveDate(entry.updated_at)}</span>
      <div className={styles.rowActions}>
        {download || view !== "legacy" ? <details className={styles.rowMenu}>
          <summary>{busy ? "Working…" : "Actions"}</summary>
          <div>
            {download ? <a aria-label={`Download ${entry.name}`} href={downloadUrl} download><DriveIcon name="download" />Download</a> : null}
            {view === "trash" ? (
              <>
                {capabilities.policy_mode !== "disabled" ? <button disabled={busy} type="button" onClick={() => onRestore(entry)}><DriveIcon name="restore" />Restore</button> : null}
                {entry.kind === "file" && capabilities.can_permanently_delete ? <button className={styles.dangerAction} disabled={busy} type="button" onClick={() => onPermanentDelete(entry)}>Delete permanently</button> : null}
              </>
            ) : view !== "legacy" ? (
              <>
                {entry.kind === "folder" && capabilities.can_manage_labels ? <button disabled={busy} type="button" onClick={() => onManageFolder(entry)}>Manage defaults</button> : null}
                {entry.kind === "file" && capabilities.can_manage_labels ? <button disabled={busy} type="button" onClick={() => onManageFile(entry)}>Move / change filing</button> : null}
                {canRescanDriveEntry(entry) && capabilities.policy_mode !== "disabled" ? <button disabled={busy} type="button" onClick={() => onRescan(entry)}><DriveIcon name="shield" />Scan again</button> : null}
                {entry.kind === "file" && capabilities.can_index ? <button disabled={busy} type="button" onClick={() => onToggleIndexing(entry)}>{entry.desired_indexed ? "Stop AI indexing" : "Index for AI"}</button> : null}
                {entry.kind === "file" && entry.malware_status === "clean" && capabilities.can_review && isPendingApproval(entry) ? <button disabled={busy} type="button" onClick={() => onApprove(entry)}>Approve for AI</button> : null}
                {capabilities.policy_mode !== "disabled" ? <button disabled={busy} type="button" onClick={() => onTrash(entry)}><DriveIcon name="trash" />Move to trash</button> : null}
              </>
            ) : null}
          </div>
        </details> : null}
      </div>
    </article>
  );
}

function isPendingApproval(entry: DriveFileEntry): boolean {
  return entry.approval_status === "pending" || ["awaiting_review", "pending"].includes(entry.index_status);
}

function emptyTitle(view: DriveView): string {
  if (view === "trash") return "Trash is empty";
  if (view === "review") return "Nothing needs review";
  if (view === "legacy") return "No existing knowledge";
  return "This folder is ready";
}

function emptyCopy(view: DriveView): string {
  if (view === "trash") return "Items moved to trash appear here until they are restored or permanently removed by policy.";
  if (view === "review") return "Files that require approval before AI can use them appear here.";
  if (view === "legacy") return "Older indexed documents without an original file appear here.";
  return "Drop files here or use New to create the first folder.";
}
