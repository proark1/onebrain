export const DRIVE_CONTRACT_VERSION = 2;

export type DriveView = "browse" | "review" | "trash" | "legacy";

export type DrivePolicyMode = "disabled" | "storage_only" | "storage_and_indexing";

export type DriveMalwareStatus =
  | "pending"
  | "scanning"
  | "clean"
  | "infected"
  | "scan_error"
  | "rescan_required";

export type DriveDepartment = {
  id: string;
  name: string;
};

export type DriveAudience = {
  classifications: string[];
  locations: string[];
  departments: DriveDepartment[];
};

export type DriveFilingPolicy = {
  classification: string;
  location: string;
  category: string;
  indexForAi: boolean;
};

export type DriveRoot = {
  id: string;
  account_id: string;
  space_id: string;
  kind: "personal" | "space" | string;
  name: string;
};

export type DriveBreadcrumb = {
  id: string;
  name: string;
};

type DriveEntryBase = {
  id: string;
  account_id: string;
  space_id: string;
  name: string;
  parent_folder_id: string;
  generation: number;
  classification: string;
  location: string;
  category: string;
  updated_at: string;
  trashed_at?: string | null;
};

export type DriveFolderEntry = DriveEntryBase & {
  kind: "folder";
  child_count?: number;
  desired_indexed: boolean;
  index_status?: "folder" | string;
};

export type DriveFileEntry = DriveEntryBase & {
  kind: "file";
  size_bytes: number;
  media_type: string;
  index_status: string;
  desired_indexed: boolean;
  approval_status?: string;
  malware_status?: DriveMalwareStatus | string;
  malware_scanned_at?: string | null;
  malware_definition_version?: string | null;
  legacy?: boolean;
  download_url?: string | null;
};

export type DriveEntry = DriveFolderEntry | DriveFileEntry;

export type DriveCapabilities = {
  can_upload: boolean;
  can_create_folder: boolean;
  can_review: boolean;
  can_manage_labels: boolean;
  can_index: boolean;
  can_permanently_delete: boolean;
  policy_mode: DrivePolicyMode;
};

export type DriveCounts = {
  review: number;
  trash: number;
  legacy: number;
};

export type DriveUploadPolicy = {
  max_file_bytes: number;
};

export type DriveBootstrap = {
  contract_version: number;
  roots: DriveRoot[];
  selected_root: DriveRoot | null;
  breadcrumbs: DriveBreadcrumb[];
  entries: DriveEntry[];
  next_cursor: string | null;
  counts: DriveCounts;
  capabilities: DriveCapabilities;
  upload: DriveUploadPolicy;
  audience?: DriveAudience;
};

export type DriveItemsResponse = {
  breadcrumbs: DriveBreadcrumb[];
  entries: DriveEntry[];
  next_cursor: string | null;
};

export type DriveUploadSession = {
  upload_id: string;
  expires_at?: string;
};

export type DriveUploadCompleteResult = {
  file: DriveFileEntry;
};

export type DriveListInput = {
  root: DriveRoot;
  folderId: string;
  view: DriveView;
  query?: string;
  cursor?: string;
  signal?: AbortSignal;
};

export type CreateDriveFolderInput = {
  root: DriveRoot;
  parentFolderId: string;
  name: string;
  idempotencyKey: string;
  policy: DriveFilingPolicy;
};

export type CreateDriveUploadInput = {
  root: DriveRoot;
  folderId: string;
  file: File;
  idempotencyKey: string;
  indexForAi: boolean;
  category: string;
};

export type UpdateDriveFolderInput = {
  folder: DriveFolderEntry;
  policy: DriveFilingPolicy;
  idempotencyKey: string;
  confirmAudienceChange: boolean;
};

export type UpdateDriveFileInput = {
  file: DriveFileEntry;
  folderId: string;
  policy: DriveFilingPolicy;
  idempotencyKey: string;
  confirmAudienceChange: boolean;
};
