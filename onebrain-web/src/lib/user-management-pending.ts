export const USER_MANAGEMENT_STATE_KEY = "onebrain.mc.users.v1";

export interface StoredUserManagementState {
  version: 1;
  deployment_id: string;
  include_deleted: boolean;
  job_id?: string;
}

export function parseUserManagementState(raw: string | null): StoredUserManagementState | null {
  if (!raw) return null;
  try {
    const value = JSON.parse(raw) as Record<string, unknown>;
    if (
      value.version !== 1
      || typeof value.deployment_id !== "string"
      || !value.deployment_id
      || typeof value.include_deleted !== "boolean"
      || (value.job_id !== undefined && (typeof value.job_id !== "string" || !value.job_id.startsWith("umj_")))
    ) return null;
    return {
      version: 1,
      deployment_id: value.deployment_id,
      include_deleted: value.include_deleted,
      ...(value.job_id ? { job_id: value.job_id as string } : {}),
    };
  } catch {
    return null;
  }
}

export function userManagementState(
  deploymentId: string,
  includeDeleted: boolean,
  jobId = "",
): StoredUserManagementState {
  return {
    version: 1,
    deployment_id: deploymentId,
    include_deleted: includeDeleted,
    ...(jobId ? { job_id: jobId } : {}),
  };
}
