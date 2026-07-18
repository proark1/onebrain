import type {
  DriveBootstrap,
  DriveBreadcrumb,
  DriveEntry,
  DriveItemsResponse,
  DriveRoot,
  DriveView,
} from "./types";

export type DriveBrowserState = {
  roots: DriveRoot[];
  selectedRoot: DriveRoot | null;
  folderId: string;
  view: DriveView;
  query: string;
  breadcrumbs: DriveBreadcrumb[];
  entries: DriveEntry[];
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
};

export type DriveUploadAction =
  | { type: "enqueue"; records: DriveUploadRecord[] }
  | { type: "status"; id: string; status: DriveUploadStatus }
  | { type: "progress"; id: string; progress: number }
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
