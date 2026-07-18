import { useRef } from "react";
import { DriveIcon } from "./drive-icons";
import type { DriveBreadcrumb, DriveRoot, DriveView } from "./types";
import styles from "./drive.module.css";

type DriveToolbarProps = {
  breadcrumbs: DriveBreadcrumb[];
  canCreateFolder: boolean;
  canUpload: boolean;
  loading: boolean;
  query: string;
  root: DriveRoot | null;
  view: DriveView;
  onChooseFiles: () => void;
  onCreateFolder: () => void;
  onNavigateFolder: (folderId: string) => void;
  onQueryChange: (query: string) => void;
  onRefresh: () => void;
};

const VIEW_TITLES: Record<DriveView, string> = {
  browse: "Files",
  review: "Needs review",
  trash: "Trash",
  legacy: "Existing knowledge",
};

export function DriveToolbar({
  breadcrumbs,
  canCreateFolder,
  canUpload,
  loading,
  query,
  root,
  view,
  onChooseFiles,
  onCreateFolder,
  onNavigateFolder,
  onQueryChange,
  onRefresh,
}: DriveToolbarProps) {
  const menuRef = useRef<HTMLDetailsElement>(null);
  function choose(action: () => void) {
    menuRef.current?.removeAttribute("open");
    action();
  }
  const title = view === "browse" ? (root?.kind === "personal" ? "My Drive" : root?.name || VIEW_TITLES[view]) : VIEW_TITLES[view];
  return (
    <header className={styles.toolbar}>
      <div className={styles.toolbarTopline}>
        <div>
          <p className={styles.eyebrow}>Drive</p>
          <h1>{title}</h1>
        </div>
        <div className={styles.toolbarActions}>
          <button className={styles.iconButton} disabled={loading} type="button" onClick={onRefresh} aria-label="Refresh files">
            <DriveIcon name="refresh" />
          </button>
          {view === "browse" && (canCreateFolder || canUpload) ? (
            <details className={styles.newMenu} ref={menuRef}>
              <summary><DriveIcon name="plus" />New</summary>
              <div>
                {canCreateFolder ? <button type="button" onClick={() => choose(onCreateFolder)}><DriveIcon name="folder" />New folder</button> : null}
                {canUpload ? <button type="button" onClick={() => choose(onChooseFiles)}><DriveIcon name="upload" />Upload files</button> : null}
              </div>
            </details>
          ) : null}
        </div>
      </div>

      <div className={styles.toolbarControls}>
        <nav className={styles.breadcrumbs} aria-label="Current folder">
          {view === "browse" ? (
            <button type="button" onClick={() => onNavigateFolder("")}>{root?.kind === "personal" ? "My Drive" : root?.name || "Files"}</button>
          ) : <span className={styles.viewCrumb}>{VIEW_TITLES[view]}</span>}
          {view === "browse" ? breadcrumbs.map((breadcrumb) => (
            <span key={breadcrumb.id}>
              <DriveIcon name="chevron" size={14} />
              <button type="button" onClick={() => onNavigateFolder(breadcrumb.id)}>{breadcrumb.name}</button>
            </span>
          )) : null}
        </nav>

        <label className={styles.searchBox}>
          <DriveIcon name="search" />
          <span className={styles.srOnly}>Search files and folders</span>
          <input
            type="search"
            placeholder="Search this location"
            value={query}
            onChange={(event) => onQueryChange(event.target.value)}
          />
        </label>
      </div>
    </header>
  );
}
