import type { ReactNode } from "react";

type DriveIconName =
  | "brain"
  | "chevron"
  | "download"
  | "file"
  | "folder"
  | "legacy"
  | "plus"
  | "refresh"
  | "restore"
  | "review"
  | "search"
  | "trash"
  | "upload";

const ICON_PATHS: Record<DriveIconName, ReactNode> = {
    brain: <><path d="M9 4.5a3 3 0 0 0-5.3 2A3.5 3.5 0 0 0 5 13.2V16a2 2 0 0 0 4 0Z" /><path d="M15 4.5a3 3 0 0 1 5.3 2 3.5 3.5 0 0 1-1.3 6.7V16a2 2 0 0 1-4 0Z" /><path d="M9 8h2m4 0h-2M9 13h2m4 0h-2m-1-9v16" /></>,
    chevron: <path d="m9 18 6-6-6-6" />,
    download: <><path d="M12 3v12m0 0 4-4m-4 4-4-4" /><path d="M5 20h14" /></>,
    file: <><path d="M6 2h8l4 4v16H6Z" /><path d="M14 2v5h5" /></>,
    folder: <path d="M3 6h7l2 2h9v11H3Z" />,
    legacy: <><path d="M4 5h16v14H4Z" /><path d="M8 9h8m-8 4h5" /></>,
    plus: <path d="M12 5v14M5 12h14" />,
    refresh: <><path d="M20 7v5h-5" /><path d="M19 12a7 7 0 1 0-2 5" /></>,
    restore: <><path d="M4 7v5h5" /><path d="M5 12a7 7 0 1 1 2 5" /></>,
    review: <><path d="M5 3h14v18H5Z" /><path d="m8 12 2 2 5-5" /></>,
    search: <><circle cx="10.5" cy="10.5" r="6.5" /><path d="m16 16 5 5" /></>,
    trash: <><path d="M4 7h16M9 3h6l1 4H8Z" /><path d="m7 7 1 14h8l1-14M10 11v6m4-6v6" /></>,
    upload: <><path d="M12 16V4m0 0L8 8m4-4 4 4" /><path d="M5 20h14" /></>,
};

export function DriveIcon({ name, size = 18 }: { name: DriveIconName; size?: number }) {
  return (
    <svg
      aria-hidden="true"
      fill="none"
      height={size}
      viewBox="0 0 24 24"
      width={size}
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth="1.7"
    >
      {ICON_PATHS[name]}
    </svg>
  );
}
