export function developmentRetryRequiresRestoreAcknowledgement(rollbackKind: string): boolean {
  return rollbackKind.trim().toLowerCase() === "restore_required";
}

export function canRetryDevelopmentRelease(
  rollbackKind: string,
  reviewNote: string,
  restoreAcknowledged: boolean,
): boolean {
  if (!developmentRetryRequiresRestoreAcknowledgement(rollbackKind)) {
    return true;
  }
  return Boolean(reviewNote.trim() && restoreAcknowledged);
}
