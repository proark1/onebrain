import type {
  DriveAudience,
  DriveBootstrap,
  DriveBreadcrumb,
  DriveEntry,
  DriveItemsResponse,
  DriveMalwareStatus,
  DriveRoot,
  DriveView,
} from "./types";

// Fallback filing audience used until a real listing arrives. Defined here — the
// pure reducer module — rather than imported, so this file keeps only type-only
// sibling imports and stays loadable by the plain `node --test` runner (which does
// not resolve extensionless value imports). The React filing controls reuse it.
export const DEFAULT_DRIVE_AUDIENCE: DriveAudience = {
  classifications: ["internal"],
  locations: ["global"],
  departments: [{ id: "general", name: "Everyone" }],
};

export type DriveBrowserState = {
  roots: DriveRoot[];
  selectedRoot: DriveRoot | null;
  folderId: string;
  view: DriveView;
  query: string;
  breadcrumbs: DriveBreadcrumb[];
  entries: DriveEntry[];
  audience: DriveAudience;
  nextCursor: string | null;
  loading: boolean;
  error: string;
  notice: string;
};

export type DriveBrowserAction =
  | { type: "select_root"; root: DriveRoot }
  | { type: "select_folder"; folderId: string }
  | { type: "select_view"; view: DriveView }
  | { type: "set_query"; query: string }
  | { type: "load_start" }
  | { type: "load_success"; response: DriveItemsResponse; append?: boolean }
  | { type: "load_error"; message: string }
  | { type: "remove_entry"; id: string }
  | { type: "replace_entry"; entry: DriveEntry }
  | { type: "merge_security"; entries: DriveEntry[] }
  | { type: "set_notice"; message: string }
  | { type: "clear_feedback" };

export function createDriveBrowserState(bootstrap: DriveBootstrap): DriveBrowserState {
  return {
    roots: bootstrap.roots,
    selectedRoot: bootstrap.selected_root ?? bootstrap.roots[0] ?? null,
    folderId: bootstrap.breadcrumbs.at(-1)?.id ?? "",
    view: "browse",
    query: "",
    breadcrumbs: bootstrap.breadcrumbs,
    entries: bootstrap.entries,
    audience: bootstrap.audience ?? DEFAULT_DRIVE_AUDIENCE,
    nextCursor: bootstrap.next_cursor,
    loading: false,
    error: "",
    notice: "",
  };
}

export function driveBrowserReducer(
  state: DriveBrowserState,
  action: DriveBrowserAction,
): DriveBrowserState {
  switch (action.type) {
    case "select_root":
      return {
        ...state,
        selectedRoot: action.root,
        folderId: "",
        view: "browse",
        query: "",
        breadcrumbs: [],
        entries: [],
        nextCursor: null,
        error: "",
        notice: "",
      };
    case "select_folder":
      return {
        ...state,
        folderId: action.folderId,
        view: "browse",
        query: "",
        entries: [],
        nextCursor: null,
        error: "",
        notice: "",
      };
    case "select_view":
      return {
        ...state,
        folderId: "",
        view: action.view,
        query: "",
        breadcrumbs: [],
        entries: [],
        nextCursor: null,
        error: "",
        notice: "",
      };
    case "set_query":
      return { ...state, query: action.query };
    case "load_start":
      return { ...state, loading: true, error: "" };
    case "load_success":
      return {
        ...state,
        breadcrumbs: action.response.breadcrumbs,
        entries: action.append ? [...state.entries, ...action.response.entries] : action.response.entries,
        // Adopt the space's current filing options; keep the last known audience if a
        // response omits it so filing controls never flash to bare defaults.
        audience: action.response.audience ?? state.audience,
        nextCursor: action.response.next_cursor,
        loading: false,
        error: "",
      };
    case "load_error":
      return { ...state, loading: false, error: action.message };
    case "remove_entry":
      return { ...state, entries: state.entries.filter((entry) => entry.id !== action.id) };
    case "replace_entry":
      return {
        ...state,
        entries: state.entries.map((entry) => (
          entry.id === action.entry.id && entry.kind === action.entry.kind ? action.entry : entry
        )),
      };
    case "merge_security": {
      const incoming = new Map(action.entries.map((entry) => [`${entry.kind}:${entry.id}`, entry]));
      let changed = false;
      const entries = state.entries.map((entry) => {
        const next = incoming.get(`${entry.kind}:${entry.id}`);
        if (!next || entry.kind !== "file" || next.kind !== "file") return entry;
        const isChanged = entry.malware_status !== next.malware_status
          || entry.malware_scanned_at !== next.malware_scanned_at
          || entry.malware_definition_version !== next.malware_definition_version
          || entry.download_url !== next.download_url
          || entry.index_status !== next.index_status
          || entry.approval_status !== next.approval_status
          || entry.updated_at !== next.updated_at;
        if (!isChanged) return entry;
        changed = true;
        return {
          ...entry,
          malware_status: next.malware_status,
          malware_scanned_at: next.malware_scanned_at,
          malware_definition_version: next.malware_definition_version,
          download_url: next.download_url,
          index_status: next.index_status,
          approval_status: next.approval_status,
          updated_at: next.updated_at,
        };
      });
      return changed ? { ...state, entries } : state;
    }
    case "set_notice":
      return { ...state, error: "", notice: action.message };
    case "clear_feedback":
      return { ...state, error: "", notice: "" };
    default:
      return state;
  }
}

export type DriveUploadStatus =
  | "queued"
  | "creating"
  | "uploading"
  | "completing"
  | "stored"
  | "failed"
  | "canceled";

export type DriveUploadRecord = {
  id: string;
  attempt: number;
  name: string;
  sizeBytes: number;
  accountId: string;
  spaceId: string;
  folderId: string;
  indexForAi: boolean;
  progress: number;
  status: DriveUploadStatus;
  error: string;
  retryable: boolean;
  fileId?: string;
  malwareStatus?: DriveMalwareStatus | string;
};

export type DriveUploadAction =
  | { type: "enqueue"; records: DriveUploadRecord[] }
  | { type: "status"; id: string; status: DriveUploadStatus }
  | { type: "progress"; id: string; progress: number }
  | { type: "completed"; id: string; fileId: string; malwareStatus?: string }
  | { type: "sync_security"; files: Array<{ id: string; malwareStatus?: string }> }
  | { type: "failed"; id: string; message: string }
  | { type: "retry"; id: string }
  | { type: "cancel"; id: string }
  | { type: "dismiss"; id: string };

export function driveUploadReducer(
  state: DriveUploadRecord[],
  action: DriveUploadAction,
): DriveUploadRecord[] {
  switch (action.type) {
    case "enqueue":
      return [...state, ...action.records];
    case "status":
      return state.map((record) => (
        record.id === action.id ? { ...record, status: action.status, error: "" } : record
      ));
    case "progress":
      return state.map((record) => (
        record.id === action.id
          ? { ...record, progress: Math.max(record.progress, Math.min(100, action.progress)) }
          : record
      ));
    case "completed":
      return state.map((record) => (
        record.id === action.id
          ? {
              ...record,
              fileId: action.fileId,
              malwareStatus: action.malwareStatus,
              progress: 100,
              status: "stored",
              error: "",
            }
          : record
      ));
    case "sync_security": {
      const statuses = new Map(action.files.map((file) => [file.id, file.malwareStatus]));
      let changed = false;
      const next = state.map((record) => {
        if (!record.fileId || !statuses.has(record.fileId)) return record;
        const malwareStatus = statuses.get(record.fileId);
        if (record.malwareStatus === malwareStatus) return record;
        changed = true;
        return { ...record, malwareStatus };
      });
      return changed ? next : state;
    }
    case "failed":
      return state.map((record) => (
        record.id === action.id ? { ...record, status: "failed", error: action.message } : record
      ));
    case "retry":
      return state.map((record) => (
        record.id === action.id && record.retryable
          ? { ...record, attempt: record.attempt + 1, status: "queued", progress: 0, error: "" }
          : record
      ));
    case "cancel":
      return state.map((record) => (
        record.id === action.id ? { ...record, status: "canceled", error: "" } : record
      ));
    case "dismiss":
      return state.filter((record) => record.id !== action.id);
    default:
      return state;
  }
}
