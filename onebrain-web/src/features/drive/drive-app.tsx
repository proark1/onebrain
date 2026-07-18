"use client";

import {
  useDeferredValue,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
  type DragEvent,
} from "react";
import {
  approveDriveFile,
  createDriveFolder,
  listDriveItems,
  mutateDriveEntry,
  permanentlyDeleteDriveFile,
  rescanDriveFile,
  setDriveFileIndexing,
  updateDriveFile,
  updateDriveFolder,
} from "./drive-client";
import {
  CreateFolderDialog,
  FileFilingDialog,
  FolderDefaultsDialog,
  PermanentDeleteDialog,
  UploadFilesDialog,
  type DriveFolderDestination,
} from "./drive-dialogs";
import { DriveEntryList } from "./drive-entry-list";
import { DriveIcon } from "./drive-icons";
import { shouldPollDriveSecurity } from "./drive-presentation";
import {
  DEFAULT_DRIVE_AUDIENCE,
  defaultDrivePolicy,
  entryDrivePolicy,
} from "./drive-policy-fields";
import { DriveSidebar } from "./drive-sidebar";
import { createDriveBrowserState, driveBrowserReducer } from "./drive-state";
import { DriveToolbar } from "./drive-toolbar";
import { DriveUploadTray, useDriveUploads } from "./drive-upload";
import {
  DRIVE_CONTRACT_VERSION,
  type DriveBootstrap,
  type DriveEntry,
  type DriveFileEntry,
  type DriveFilingPolicy,
  type DriveFolderEntry,
  type DriveRoot,
  type DriveView,
} from "./types";
import styles from "./drive.module.css";

const EMPTY_BOOTSTRAP: DriveBootstrap = {
  contract_version: DRIVE_CONTRACT_VERSION,
  roots: [],
  selected_root: null,
  breadcrumbs: [],
  entries: [],
  next_cursor: null,
  counts: { review: 0, trash: 0, legacy: 0 },
  capabilities: {
    can_upload: false,
    can_create_folder: false,
    can_review: false,
    can_manage_labels: false,
    can_index: false,
    can_permanently_delete: false,
    policy_mode: "disabled",
  },
  upload: { max_file_bytes: 0 },
  audience: DEFAULT_DRIVE_AUDIENCE,
};

type UploadRequest = { files: File[]; folderId: string };

const DRIVE_SECURITY_POLL_LIMIT = 24;

export function DriveApp({
  initialBootstrap,
  initialError = "",
}: {
  initialBootstrap?: DriveBootstrap;
  initialError?: string;
}) {
  const bootstrap = initialBootstrap ?? EMPTY_BOOTSTRAP;
  const capabilities = bootstrap.capabilities;
  const audience = bootstrap.audience ?? DEFAULT_DRIVE_AUDIENCE;
  const [state, dispatch] = useReducer(driveBrowserReducer, bootstrap, createDriveBrowserState);
  const [counts, setCounts] = useState(bootstrap.counts);
  const [reloadToken, setReloadToken] = useState(0);
  const [actionId, setActionId] = useState("");
  const [createFolderOpen, setCreateFolderOpen] = useState(false);
  const [folderToManage, setFolderToManage] = useState<DriveFolderEntry | null>(null);
  const [fileToManage, setFileToManage] = useState<DriveFileEntry | null>(null);
  const [fileToDelete, setFileToDelete] = useState<DriveFileEntry | null>(null);
  const [uploadRequest, setUploadRequest] = useState<UploadRequest | null>(null);
  const [draggingFiles, setDraggingFiles] = useState(false);
  const dragDepthRef = useRef(0);
  const requestSequenceRef = useRef(0);
  const uploadRefreshRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const deferredQuery = useDeferredValue(state.query);
  const initialKey = loadKey(state.selectedRoot, state.folderId, state.view, "", 0);
  const lastLoadKeyRef = useRef(initialKey);

  const uploads = useDriveUploads({
    maxFileBytes: bootstrap.upload.max_file_bytes,
    onComplete: () => {
      if (uploadRefreshRef.current) clearTimeout(uploadRefreshRef.current);
      uploadRefreshRef.current = setTimeout(() => setReloadToken((current) => current + 1), 180);
    },
  });
  const syncUploadSecurity = uploads.syncSecurity;

  const destinations = useMemo(
    () => driveDestinations(state.selectedRoot, state.breadcrumbs, state.entries, audience, capabilities.can_index),
    [audience, capabilities.can_index, state.breadcrumbs, state.entries, state.selectedRoot],
  );
  const securityPollKey = useMemo(
    () => Array.from(new Set(
      state.entries.filter(shouldPollDriveSecurity).map((entry) => entry.id),
    )).sort().join("|"),
    [state.entries],
  );
  const pollRootId = state.selectedRoot?.id ?? "";
  const pollAccountId = state.selectedRoot?.account_id ?? "";
  const pollSpaceId = state.selectedRoot?.space_id ?? "";
  const pollRootKind = state.selectedRoot?.kind ?? "space";
  const pollRootName = state.selectedRoot?.name ?? "Drive";

  useEffect(() => () => {
    if (uploadRefreshRef.current) clearTimeout(uploadRefreshRef.current);
  }, []);

  useEffect(() => {
    syncUploadSecurity(state.entries.flatMap((entry) => entry.kind === "file" ? [entry] : []));
  }, [state.entries, syncUploadSecurity]);

  useEffect(() => {
    const root = state.selectedRoot;
    if (!root || bootstrap.contract_version !== DRIVE_CONTRACT_VERSION) return;
    const key = loadKey(root, state.folderId, state.view, deferredQuery, reloadToken);
    if (key === lastLoadKeyRef.current) return;
    lastLoadKeyRef.current = key;
    const sequence = requestSequenceRef.current + 1;
    requestSequenceRef.current = sequence;
    const controller = new AbortController();
    dispatch({ type: "load_start" });
    void listDriveItems({
      root,
      folderId: state.folderId,
      view: state.view,
      query: deferredQuery,
      signal: controller.signal,
    }).then((response) => {
      if (sequence === requestSequenceRef.current) dispatch({ type: "load_success", response });
    }).catch((err) => {
      if (controller.signal.aborted || sequence !== requestSequenceRef.current) return;
      dispatch({ type: "load_error", message: messageFrom(err, "Could not load this Drive location.") });
    });
    return () => controller.abort();
  }, [bootstrap.contract_version, deferredQuery, reloadToken, state.folderId, state.selectedRoot, state.view]);

  useEffect(() => {
    if (!securityPollKey || !pollRootId || !pollAccountId || !pollSpaceId) return;
    let stopped = false;
    let attempts = 0;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let controller: AbortController | null = null;

    const poll = async () => {
      attempts += 1;
      controller = new AbortController();
      try {
        const response = await listDriveItems({
          root: {
            id: pollRootId,
            account_id: pollAccountId,
            space_id: pollSpaceId,
            kind: pollRootKind,
            name: pollRootName,
          },
          folderId: state.folderId,
          view: state.view,
          query: deferredQuery,
          signal: controller.signal,
        });
        if (!stopped) dispatch({ type: "merge_security", entries: response.entries });
      } catch {
        // The row remains quarantined. Manual refresh remains available if polling exhausts.
      } finally {
        controller = null;
        if (!stopped && attempts < DRIVE_SECURITY_POLL_LIMIT) {
          timer = setTimeout(() => void poll(), securityPollDelay(attempts));
        }
      }
    };

    timer = setTimeout(() => void poll(), securityPollDelay(0));
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
      controller?.abort();
    };
  }, [
    deferredQuery,
    pollAccountId,
    pollRootId,
    pollRootKind,
    pollRootName,
    pollSpaceId,
    reloadToken,
    securityPollKey,
    state.folderId,
    state.view,
  ]);

  if (bootstrap.contract_version !== DRIVE_CONTRACT_VERSION) {
    return <DriveUnavailable title="Drive update required" message="The Drive interface and Core API use different contract versions. Finish the Core rollout, then refresh." />;
  }

  if (initialError) {
    return <DriveUnavailable title="Drive could not load" message={initialError} />;
  }

  function selectRoot(root: DriveRoot) {
    dispatch({ type: "select_root", root });
  }

  function selectView(view: DriveView) {
    dispatch({ type: "select_view", view });
  }

  function selectFolder(folderId: string) {
    dispatch({ type: "select_folder", folderId });
  }

  function refresh() {
    setReloadToken((current) => current + 1);
  }

  async function loadMore() {
    if (!state.selectedRoot || !state.nextCursor || state.loading) return;
    const sequence = requestSequenceRef.current + 1;
    requestSequenceRef.current = sequence;
    dispatch({ type: "load_start" });
    try {
      const response = await listDriveItems({
        root: state.selectedRoot,
        folderId: state.folderId,
        view: state.view,
        query: deferredQuery,
        cursor: state.nextCursor,
      });
      if (sequence === requestSequenceRef.current) dispatch({ type: "load_success", response, append: true });
    } catch (err) {
      if (sequence === requestSequenceRef.current) dispatch({ type: "load_error", message: messageFrom(err, "Could not load more files.") });
    }
  }

  async function createFolder(name: string, idempotencyKey: string, policy: DriveFilingPolicy) {
    if (!state.selectedRoot) throw new Error("Choose a Drive space before creating a folder.");
    await createDriveFolder({ root: state.selectedRoot, parentFolderId: state.folderId, name, idempotencyKey, policy });
    dispatch({ type: "set_notice", message: `${name} created with filing defaults.` });
    refresh();
  }

  async function saveFolderDefaults(
    folder: DriveFolderEntry,
    policy: DriveFilingPolicy,
    idempotencyKey: string,
    confirmAudienceChange: boolean,
  ) {
    const updated = await updateDriveFolder({ folder, policy, idempotencyKey, confirmAudienceChange });
    dispatch({ type: "replace_entry", entry: updated });
    dispatch({ type: "set_notice", message: `${folder.name} defaults updated.` });
  }

  async function saveFileFiling(
    file: DriveFileEntry,
    folderId: string,
    policy: DriveFilingPolicy,
    idempotencyKey: string,
    confirmAudienceChange: boolean,
  ) {
    const updated = await updateDriveFile({ file, folderId, policy, idempotencyKey, confirmAudienceChange });
    if (state.view === "browse" && folderId !== state.folderId) dispatch({ type: "remove_entry", id: file.id });
    else dispatch({ type: "replace_entry", entry: updated });
    dispatch({ type: "set_notice", message: `${file.name} filing updated; AI visibility is being reconciled.` });
  }

  async function changeTrashState(entry: DriveEntry, action: "trash" | "restore") {
    setActionId(entry.id);
    dispatch({ type: "clear_feedback" });
    try {
      await mutateDriveEntry(entry, action);
      dispatch({ type: "remove_entry", id: entry.id });
      dispatch({ type: "set_notice", message: action === "trash" ? `${entry.name} moved to trash and removed from AI.` : `${entry.name} restored.` });
      setCounts((current) => ({ ...current, trash: Math.max(0, current.trash + (action === "trash" ? 1 : -1)) }));
    } catch (err) {
      dispatch({ type: "load_error", message: messageFrom(err, `Could not ${action} ${entry.name}.`) });
    } finally {
      setActionId("");
    }
  }

  async function toggleIndexing(file: DriveFileEntry) {
    setActionId(file.id);
    dispatch({ type: "clear_feedback" });
    try {
      const enabled = !file.desired_indexed;
      const updated = await setDriveFileIndexing(file, enabled);
      dispatch({ type: "replace_entry", entry: updated });
      dispatch({ type: "set_notice", message: enabled ? `${file.name} queued for policy checks and indexing.` : `${file.name} removed from AI use.` });
    } catch (err) {
      dispatch({ type: "load_error", message: messageFrom(err, "Could not change AI indexing.") });
    } finally {
      setActionId("");
    }
  }

  async function approve(file: DriveFileEntry) {
    setActionId(file.id);
    dispatch({ type: "clear_feedback" });
    try {
      const updated = await approveDriveFile(file);
      if (state.view === "review") dispatch({ type: "remove_entry", id: file.id });
      else dispatch({ type: "replace_entry", entry: updated });
      setCounts((current) => ({ ...current, review: Math.max(0, current.review - 1) }));
      dispatch({ type: "set_notice", message: `${file.name} approved and queued for indexing.` });
    } catch (err) {
      dispatch({ type: "load_error", message: messageFrom(err, "Could not approve the file.") });
    } finally {
      setActionId("");
    }
  }

  async function rescan(file: DriveFileEntry) {
    setActionId(file.id);
    dispatch({ type: "clear_feedback" });
    try {
      const updated = await rescanDriveFile(file);
      dispatch({ type: "replace_entry", entry: updated });
      dispatch({ type: "set_notice", message: `${file.name} queued for another security scan.` });
    } catch (err) {
      dispatch({ type: "load_error", message: messageFrom(err, "Could not start another security scan.") });
    } finally {
      setActionId("");
    }
  }

  async function permanentlyDelete(file: DriveFileEntry, reason: string) {
    setActionId(file.id);
    try {
      await permanentlyDeleteDriveFile(file, reason);
      dispatch({ type: "remove_entry", id: file.id });
      setCounts((current) => ({ ...current, trash: Math.max(0, current.trash - 1) }));
      dispatch({ type: "set_notice", message: `${file.name} was permanently deleted.` });
    } finally {
      setActionId("");
    }
  }

  function requestUpload(files: File[], folderId = state.folderId) {
    dragDepthRef.current = 0;
    setDraggingFiles(false);
    if (files.length || capabilities.can_upload) setUploadRequest({ files, folderId });
  }

  function startUpload(files: File[], indexForAi: boolean) {
    if (!uploadRequest) return;
    uploads.enqueue(files, state.selectedRoot, uploadRequest.folderId, indexForAi);
  }

  function dragEnter(event: DragEvent<HTMLDivElement>) {
    if (!capabilities.can_upload || state.view !== "browse" || !event.dataTransfer.types.includes("Files")) return;
    event.preventDefault();
    dragDepthRef.current += 1;
    setDraggingFiles(true);
  }

  function dragLeave(event: DragEvent<HTMLDivElement>) {
    if (!draggingFiles) return;
    event.preventDefault();
    dragDepthRef.current = Math.max(0, dragDepthRef.current - 1);
    if (dragDepthRef.current === 0) setDraggingFiles(false);
  }

  function dropFiles(event: DragEvent<HTMLDivElement>) {
    if (!draggingFiles) return;
    event.preventDefault();
    requestUpload(Array.from(event.dataTransfer.files));
  }

  if (!state.selectedRoot) {
    return <DriveUnavailable title="No Drive space available" message="Ask an administrator to add you to a company space or create your personal Drive." />;
  }

  const uploadDestination = destinationName(uploadRequest?.folderId ?? state.folderId, state.selectedRoot, state.breadcrumbs, state.entries);

  return (
    <div className={styles.driveWorkspace} onDragEnter={dragEnter} onDragLeave={dragLeave} onDragOver={(event) => { if (draggingFiles) event.preventDefault(); }} onDrop={dropFiles}>
      <DriveSidebar capabilities={capabilities} counts={counts} roots={state.roots} selectedRoot={state.selectedRoot} view={state.view} onSelectRoot={selectRoot} onSelectView={selectView} />

      <div className={styles.browser}>
        <DriveToolbar
          breadcrumbs={state.breadcrumbs}
          canCreateFolder={capabilities.can_create_folder}
          canUpload={capabilities.can_upload}
          loading={state.loading}
          query={state.query}
          root={state.selectedRoot}
          view={state.view}
          onChooseFiles={() => requestUpload([])}
          onCreateFolder={() => setCreateFolderOpen(true)}
          onNavigateFolder={selectFolder}
          onQueryChange={(query) => dispatch({ type: "set_query", query })}
          onRefresh={refresh}
        />

        <DrivePolicyBanner mode={capabilities.policy_mode} />
        {state.error ? <div className={styles.errorNotice} role="alert"><span>{state.error}</span><button type="button" onClick={refresh}>Retry</button></div> : null}
        {state.notice ? <div className={styles.successNotice} role="status">{state.notice}</div> : null}

        <DriveEntryList
          actionId={actionId}
          capabilities={capabilities}
          entries={state.entries}
          loading={state.loading}
          nextCursor={state.nextCursor}
          view={state.view}
          onApprove={(file) => void approve(file)}
          onDropFiles={requestUpload}
          onLoadMore={() => void loadMore()}
          onManageFile={setFileToManage}
          onManageFolder={setFolderToManage}
          onOpenFolder={selectFolder}
          onPermanentDelete={setFileToDelete}
          onRescan={(file) => void rescan(file)}
          onRestore={(entry) => void changeTrashState(entry, "restore")}
          onToggleIndexing={(file) => void toggleIndexing(file)}
          onTrash={(entry) => void changeTrashState(entry, "trash")}
        />
      </div>

      {draggingFiles ? <div className={styles.dragOverlay} aria-hidden="true"><DriveIcon name="upload" size={32} /><strong>Drop to upload here</strong><span>You will choose whether AI may index these files.</span></div> : null}

      {createFolderOpen ? <CreateFolderDialog audience={audience} canIndex={capabilities.can_index} open policyMode={capabilities.policy_mode} onClose={() => setCreateFolderOpen(false)} onCreate={createFolder} /> : null}
      {folderToManage ? <FolderDefaultsDialog audience={audience} canConfirmWidening={capabilities.can_permanently_delete} canIndex={capabilities.can_index} folder={folderToManage} open policyMode={capabilities.policy_mode} onClose={() => setFolderToManage(null)} onSave={saveFolderDefaults} /> : null}
      {fileToManage ? <FileFilingDialog audience={audience} canConfirmWidening={capabilities.can_permanently_delete} canIndex={capabilities.can_index} destinations={destinations} file={fileToManage} open policyMode={capabilities.policy_mode} onClose={() => setFileToManage(null)} onSave={saveFileFiling} /> : null}
      {uploadRequest ? <UploadFilesDialog canIndex={capabilities.can_index} destinationName={uploadDestination} open policyMode={capabilities.policy_mode} preselectedFiles={uploadRequest.files} onClose={() => setUploadRequest(null)} onUpload={startUpload} /> : null}
      {fileToDelete ? <PermanentDeleteDialog file={fileToDelete} open onClose={() => setFileToDelete(null)} onDelete={permanentlyDelete} /> : null}
      <DriveUploadTray uploads={uploads.uploads} onCancel={uploads.cancel} onDismiss={uploads.dismiss} onRetry={uploads.retry} />
    </div>
  );
}

function DrivePolicyBanner({ mode }: { mode: DriveBootstrap["capabilities"]["policy_mode"] }) {
  if (mode === "storage_and_indexing") return null;
  return (
    <div className={styles.policyBanner} role="status">
      <DriveIcon name="brain" />
      <span>{mode === "storage_only" ? "Storage-only mode: files remain governed and become downloadable after security scanning, but AI indexing is disabled for this deployment." : "Drive is disabled for this deployment. Existing content is read-only and no uploads or filing changes are available."}</span>
    </div>
  );
}

function DriveUnavailable({ title, message }: { title: string; message: string }) {
  return <section className={styles.unavailable}><DriveIcon name="folder" size={32} /><p className={styles.eyebrow}>Drive</p><h1>{title}</h1><p>{message}</p></section>;
}

function driveDestinations(
  root: DriveRoot | null,
  breadcrumbs: { id: string; name: string }[],
  entries: DriveEntry[],
  audience: NonNullable<DriveBootstrap["audience"]>,
  canIndex: boolean,
): DriveFolderDestination[] {
  if (!root) return [];
  const items: DriveFolderDestination[] = [{ id: "", name: `${root.name} (root)`, policy: defaultDrivePolicy(audience, canIndex) }];
  items.push(...breadcrumbs.map((item) => ({ id: item.id, name: item.name })));
  items.push(...entries.flatMap((entry) => entry.kind === "folder" ? [{ id: entry.id, name: entry.name, policy: entryDrivePolicy(entry) }] : []));
  return Array.from(new Map(items.map((item) => [item.id, item])).values());
}

function destinationName(folderId: string, root: DriveRoot, breadcrumbs: { id: string; name: string }[], entries: DriveEntry[]): string {
  if (!folderId) return root.name;
  return entries.find((entry) => entry.kind === "folder" && entry.id === folderId)?.name
    ?? breadcrumbs.find((item) => item.id === folderId)?.name
    ?? "this folder";
}

function loadKey(root: DriveRoot | null, folderId: string, view: DriveView, query: string, token: number): string {
  return `${root?.id ?? "none"}:${folderId}:${view}:${query.trim()}:${token}`;
}

function messageFrom(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

function securityPollDelay(attempt: number): number {
  if (attempt < 4) return 1_500;
  if (attempt < 10) return 3_000;
  return 6_000;
}
