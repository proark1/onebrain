import { DriveIcon } from "./drive-icons";
import {
  driveSecurityPresentation,
  driveStatusPresentation,
  type DriveStatusTone,
} from "./drive-presentation";
import styles from "./drive.module.css";

const TONE_CLASSES: Record<DriveStatusTone, string> = {
  neutral: styles.statusNeutral,
  running: styles.statusRunning,
  success: styles.statusSuccess,
  warning: styles.statusWarning,
  danger: styles.statusDanger,
};

export function DriveAiStatus({ status }: { status: string }) {
  const presentation = driveStatusPresentation(status);
  return <DriveStatus icon="brain" presentation={presentation} />;
}

export function DriveSecurityStatus({ status }: { status?: string }) {
  const presentation = driveSecurityPresentation(status);
  return <DriveStatus icon="shield" presentation={presentation} />;
}

function DriveStatus({
  icon,
  presentation,
}: {
  icon: "brain" | "shield";
  presentation: ReturnType<typeof driveStatusPresentation>;
}) {
  return (
    <span
      className={`${styles.statusChip} ${TONE_CLASSES[presentation.tone]}`}
      title={presentation.detail}
    >
      <span className={styles.statusRail} aria-hidden="true" />
      <DriveIcon name={icon} size={14} />
      <span>{presentation.label}</span>
      <span className={styles.srOnly}>{presentation.detail}</span>
    </span>
  );
}
