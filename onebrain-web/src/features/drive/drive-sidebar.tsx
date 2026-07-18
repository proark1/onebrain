import { DriveIcon } from "./drive-icons";
import type { DriveCapabilities, DriveCounts, DriveRoot, DriveView } from "./types";
import styles from "./drive.module.css";

type DriveSidebarProps = {
  capabilities: DriveCapabilities;
  counts: DriveCounts;
  roots: DriveRoot[];
  selectedRoot: DriveRoot | null;
  view: DriveView;
  onSelectRoot: (root: DriveRoot) => void;
  onSelectView: (view: DriveView) => void;
};

export function DriveSidebar({
  capabilities,
  counts,
  roots,
  selectedRoot,
  view,
  onSelectRoot,
  onSelectView,
}: DriveSidebarProps) {
  const personal = roots.filter((root) => root.kind === "personal");
  const spaces = roots.filter((root) => root.kind !== "personal");
  return (
    <aside className={styles.sidebar} aria-label="Drive locations">
      <nav className={styles.rootNav} aria-label="Files">
        <p className={styles.railLabel}>Files</p>
        {personal.map((root) => (
          <RootButton key={root.id} root={root} selected={view === "browse" && selectedRoot?.id === root.id} onSelect={onSelectRoot} />
        ))}
        {spaces.length ? <p className={styles.railLabel}>Spaces</p> : null}
        {spaces.map((root) => (
          <RootButton key={root.id} root={root} selected={view === "browse" && selectedRoot?.id === root.id} onSelect={onSelectRoot} />
        ))}
      </nav>

      <nav className={styles.viewNav} aria-label="Drive views">
        {capabilities.can_review ? (
          <ViewButton icon="review" label="Needs review" count={counts.review} selected={view === "review"} onClick={() => onSelectView("review")} />
        ) : null}
        <ViewButton icon="trash" label="Trash" count={counts.trash} selected={view === "trash"} onClick={() => onSelectView("trash")} />
        {counts.legacy > 0 ? (
          <ViewButton icon="legacy" label="Existing knowledge" count={counts.legacy} selected={view === "legacy"} onClick={() => onSelectView("legacy")} />
        ) : null}
      </nav>
    </aside>
  );
}

function RootButton({
  root,
  selected,
  onSelect,
}: {
  root: DriveRoot;
  selected: boolean;
  onSelect: (root: DriveRoot) => void;
}) {
  return (
    <button
      aria-current={selected ? "page" : undefined}
      className={selected ? styles.railButtonActive : styles.railButton}
      type="button"
      onClick={() => onSelect(root)}
    >
      <DriveIcon name="folder" />
      <span>{root.kind === "personal" ? "My Drive" : root.name}</span>
    </button>
  );
}

function ViewButton({
  count,
  icon,
  label,
  selected,
  onClick,
}: {
  count: number;
  icon: "legacy" | "review" | "trash";
  label: string;
  selected: boolean;
  onClick: () => void;
}) {
  return (
    <button
      aria-current={selected ? "page" : undefined}
      className={selected ? styles.railButtonActive : styles.railButton}
      type="button"
      onClick={onClick}
    >
      <DriveIcon name={icon} />
      <span>{label}</span>
      {count > 0 ? <small>{count}</small> : null}
    </button>
  );
}
