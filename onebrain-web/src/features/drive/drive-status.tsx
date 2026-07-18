import { DriveIcon } from "./drive-icons";
import { driveStatusPresentation, type DriveStatusTone } from "./drive-presentation";
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
  return (
    <span
      className={`${styles.aiStatus} ${TONE_CLASSES[presentation.tone]}`}
      title={presentation.detail}
    >
      <span className={styles.aiRail} aria-hidden="true" />
      <DriveIcon name="brain" size={14} />
      <span>{presentation.label}</span>
      <span className={styles.srOnly}>{presentation.detail}</span>
    </span>
  );
}
