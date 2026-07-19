"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { Notice, PageHeader, Panel, StatusBadge } from "@/components/admin-ui";
import { Timestamp } from "@/components/operational/timestamp";
import {
  createManagedUser,
  deleteManagedUser,
  disableManagedUser,
  enableManagedUser,
  getFleetOverview,
  getUserManagementJob,
  refreshManagedUserDirectory,
  resetManagedUserPassword,
  revealManagedUserSecret,
} from "@/lib/onebrain-client";
import type {
  FleetDeploymentOverview,
  ManagedUser,
  ManagedUserDirectory,
  ManagedUserMutationResult,
  UserManagementJob,
} from "@/lib/onebrain-types";
import {
  parseUserManagementState,
  userManagementState,
  USER_MANAGEMENT_STATE_KEY,
} from "@/lib/user-management-pending";

const TERMINAL = new Set(["completed", "failed", "expired"]);
const POLLING_PAUSED = Symbol("user-management-polling-paused");

const ERROR_COPY: Record<string, string> = {
  capability_unavailable: "This server must be upgraded before MC can manage its users.",
  command_expired: "The server did not collect this request before it expired. Check its health, then retry.",
  duplicate_email: "An account already uses that email address on this server.",
  invalid_location: "That location is not valid for the selected role.",
  invalid_role: "That role is no longer available on this server. Refresh the directory.",
  invalid_state_transition: "The account changed while this request was waiting. Refresh and try again.",
  last_active_admin: "This is the server’s last active administrator and cannot be disabled.",
  ownership_reassignment_required: "Reassign this user’s owned workspaces before deleting the account.",
  recent_authentication_required: "Sign in to Mission Control again before changing an account.",
  secret_already_consumed: "That one-time password was already shown or has expired. Reset it again if needed.",
  user_not_found: "That account no longer exists on this server.",
};

function errorCopy(value: unknown): string {
  const message = value instanceof Error ? value.message : String(value || "Request failed");
  return ERROR_COPY[message] || message;
}

function statusTone(status: ManagedUser["status"]): "danger" | "neutral" | "success" | "warning" {
  if (status === "active") return "success";
  if (status === "disabled") return "warning";
  return "neutral";
}

function wait(delay: number) {
  return new Promise((resolve) => window.setTimeout(resolve, delay));
}

function readStoredState() {
  try {
    return parseUserManagementState(window.sessionStorage.getItem(USER_MANAGEMENT_STATE_KEY));
  } catch {
    return null;
  }
}

function storeState(deploymentId: string, includeDeleted: boolean, jobId = "") {
  if (!deploymentId) return;
  try {
    window.sessionStorage.setItem(
      USER_MANAGEMENT_STATE_KEY,
      JSON.stringify(userManagementState(deploymentId, includeDeleted, jobId)),
    );
  } catch {
    // A blocked session store must not block account recovery.
  }
}

function clearStoredJob(jobId: string, deploymentId: string, includeDeleted: boolean) {
  const current = readStoredState();
  if (!current || current.job_id === jobId) storeState(deploymentId, includeDeleted);
}

export function UsersPanel() {
  const mounted = useRef(true);
  const [deployments, setDeployments] = useState<FleetDeploymentOverview[]>([]);
  const [deploymentId, setDeploymentId] = useState("");
  const [directory, setDirectory] = useState<ManagedUserDirectory | null>(null);
  const [pending, setPending] = useState<UserManagementJob<unknown> | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [loadingFleet, setLoadingFleet] = useState(true);
  const [includeDeleted, setIncludeDeleted] = useState(false);
  const [secret, setSecret] = useState<{ label: string; value: string } | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<ManagedUser | null>(null);
  const [deleteConfirmation, setDeleteConfirmation] = useState("");
  const [draft, setDraft] = useState({ display_name: "", email: "", role_id: "", location: "" });

  const selectedDeployment = deployments.find((deployment) => deployment.deployment_id === deploymentId) || null;
  const selectedRole = directory?.roles.find((role) => role.id === draft.role_id) || null;
  const busy = pending !== null && !TERMINAL.has(pending.status);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  useEffect(() => {
    let active = true;
    getFleetOverview()
      .then((overview) => {
        if (active) setDeployments(overview.deployments);
      })
      .catch((reason) => {
        if (active) setError(errorCopy(reason));
      })
      .finally(() => {
        if (active) setLoadingFleet(false);
      });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    const stored = readStoredState();
    if (!stored) return;
    const { deployment_id: storedDeploymentId, include_deleted: storedIncludeDeleted, job_id: storedJobId } = stored;
    let cancelled = false;
    window.queueMicrotask(() => {
      if (!cancelled) {
        setDeploymentId(storedDeploymentId);
        setIncludeDeleted(storedIncludeDeleted);
      }
    });
    if (!storedJobId) return () => { cancelled = true; };
    const resumableJobId = storedJobId;
    let activeJobId = resumableJobId;

    async function poll<T>(created: UserManagementJob<T>) {
      let job = created;
      activeJobId = job.id;
      storeState(job.deployment_id, storedIncludeDeleted, job.id);
      if (!cancelled) setPending(job);
      for (let attempt = 0; attempt < 180 && !TERMINAL.has(job.status); attempt += 1) {
        await wait(1_000);
        if (cancelled) throw POLLING_PAUSED;
        job = await getUserManagementJob<T>(job.id);
        if (cancelled) throw POLLING_PAUSED;
        setPending(job);
      }
      if (!TERMINAL.has(job.status)) throw new Error("The server has not completed this request yet.");
      return job;
    }

    async function restore() {
      try {
        const job = await poll(await getUserManagementJob(resumableJobId));
        let finalJobId = job.id;
        if (job.status !== "completed") throw new Error(job.error_code || "Account request failed.");

        if (job.action === "directory.snapshot") {
          const result = job.result as UserManagementJob<ManagedUserDirectory>["result"];
          if (!result?.ok || !result.data) throw new Error(result?.error_code || "Directory refresh failed.");
          setDirectory(result.data);
          setDraft((current) => ({ ...current, role_id: current.role_id || result.data?.roles[0]?.id || "" }));
        } else {
          if (job.action === "user.create" || job.action === "user.password.reset") {
            const revealed = await revealManagedUserSecret(job.id);
            const password = revealed.data?.one_time_password;
            if (!revealed.ok || !password) throw new Error(revealed.error_code || "One-time password unavailable.");
            setSecret({ label: "One-time password", value: password });
          }
          setNotice("The account request completed while you were away.");
          const directoryJob = await poll(await refreshManagedUserDirectory(storedDeploymentId, storedIncludeDeleted));
          finalJobId = directoryJob.id;
          const result = directoryJob.result;
          if (directoryJob.status !== "completed" || !result?.ok || !result.data) {
            throw new Error(directoryJob.error_code || result?.error_code || "Directory refresh failed.");
          }
          setDirectory(result.data);
          setDraft((current) => ({ ...current, role_id: current.role_id || result.data?.roles[0]?.id || "" }));
        }
        clearStoredJob(finalJobId, storedDeploymentId, storedIncludeDeleted);
        setPending(null);
      } catch (reason) {
        if (reason === POLLING_PAUSED) return;
        clearStoredJob(activeJobId, storedDeploymentId, storedIncludeDeleted);
        setPending(null);
        setError(errorCopy(reason));
      }
    }
    void restore();
    return () => { cancelled = true; };
  }, []);

  async function awaitJob<T>(created: UserManagementJob<T>): Promise<UserManagementJob<T>> {
    let job = created;
    storeState(job.deployment_id, includeDeleted, job.id);
    if (!mounted.current) throw POLLING_PAUSED;
    setPending(job);
    for (let attempt = 0; attempt < 180 && !TERMINAL.has(job.status); attempt += 1) {
      await wait(1_000);
      if (!mounted.current) throw POLLING_PAUSED;
      job = await getUserManagementJob<T>(job.id);
      if (!mounted.current) throw POLLING_PAUSED;
      setPending(job);
    }
    if (!TERMINAL.has(job.status)) throw new Error("The server has not completed this request yet.");
    return job;
  }

  async function loadDirectory(targetId: string, showDeleted: boolean, preserveFeedback = false) {
    if (!targetId) return;
    setError("");
    if (!preserveFeedback) {
      setNotice("");
      setSecret(null);
    }
    try {
      const job = await awaitJob(await refreshManagedUserDirectory(targetId, showDeleted));
      if (job.status !== "completed" || !job.result?.ok || !job.result.data) {
        throw new Error(job.error_code || job.result?.error_code || "Directory refresh failed.");
      }
      setDirectory(job.result.data);
      setDraft((current) => ({
        ...current,
        role_id: current.role_id || job.result?.data?.roles[0]?.id || "",
      }));
      clearStoredJob(job.id, targetId, showDeleted);
      setPending(null);
    } catch (reason) {
      if (reason === POLLING_PAUSED) return;
      const jobId = readStoredState()?.job_id || "";
      if (jobId) clearStoredJob(jobId, targetId, showDeleted);
      setPending(null);
      setError(errorCopy(reason));
    }
  }

  function chooseDeployment(value: string) {
    setDeploymentId(value);
    setDirectory(null);
    setPending(null);
    setSecret(null);
    setDeleteTarget(null);
    setDeleteConfirmation("");
    setError("");
    setNotice("");
    if (value) storeState(value, includeDeleted);
    else {
      try { window.sessionStorage.removeItem(USER_MANAGEMENT_STATE_KEY); } catch { /* no-op */ }
    }
  }

  async function runMutation(
    start: () => Promise<UserManagementJob<ManagedUserMutationResult>>,
    success: string,
    revealLabel = "",
  ) {
    setError("");
    setNotice("");
    setSecret(null);
    try {
      const job = await awaitJob(await start());
      if (job.status !== "completed") throw new Error(job.error_code || "Account change failed.");
      if (revealLabel) {
        const revealed = await revealManagedUserSecret(job.id);
        const password = revealed.data?.one_time_password;
        if (!revealed.ok || !password) throw new Error(revealed.error_code || "One-time password unavailable.");
        setSecret({ label: revealLabel, value: password });
      }
      setNotice(success);
      clearStoredJob(job.id, deploymentId, includeDeleted);
      setPending(null);
      await loadDirectory(deploymentId, includeDeleted, true);
    } catch (reason) {
      if (reason === POLLING_PAUSED) return;
      const jobId = readStoredState()?.job_id || "";
      if (jobId) clearStoredJob(jobId, deploymentId, includeDeleted);
      setPending(null);
      setError(errorCopy(reason));
    }
  }

  async function onCreate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await runMutation(
      () => createManagedUser(deploymentId, draft),
      `Created ${draft.email}. Copy the one-time password now.`,
      `One-time password for ${draft.email}`,
    );
    setDraft((current) => ({ ...current, display_name: "", email: "" }));
  }

  async function copySecret() {
    if (!secret) return;
    try {
      await navigator.clipboard.writeText(secret.value);
      setNotice("Copied the one-time password.");
    } catch {
      setError("Copy was blocked by the browser. Select the password and copy it manually.");
    }
  }

  async function confirmDelete() {
    if (!deleteTarget || deleteConfirmation !== deleteTarget.email) return;
    const email = deleteTarget.email;
    await runMutation(
      () => deleteManagedUser(deploymentId, deleteTarget.id),
      `Deleted and anonymized ${email}.`,
    );
    setDeleteTarget(null);
    setDeleteConfirmation("");
  }

  return (
    <div className="adminSurface userManagementSurface">
      <PageHeader
        eyebrow="Mission Control"
        title="Users"
        meta="Account access and recovery, one server at a time"
      />

      {error ? <Notice tone="error">{error}</Notice> : null}
      {notice ? <Notice tone="success">{notice}</Notice> : null}
      {secret ? (
        <section className="oneTimeReceipt" aria-live="polite">
          <div><span>Shown once</span><strong>{secret.label}</strong></div>
          <code>{secret.value}</code>
          <div className="oneTimeReceiptActions">
            <button type="button" onClick={() => void copySecret()}>Copy</button>
            <button className="secondaryButton" type="button" onClick={() => setSecret(null)}>I have saved it</button>
          </div>
        </section>
      ) : null}

      <section className="userServerRail" aria-label="Managed server">
        <div className="userServerGlyph" aria-hidden="true"><span /><span /><span /></div>
        <label>
          <span>Customer server</span>
          <select disabled={loadingFleet || busy} value={deploymentId} onChange={(event) => chooseDeployment(event.target.value)}>
            <option value="">Select a server</option>
            {deployments.map((deployment) => (
              <option key={deployment.deployment_id} value={deployment.deployment_id}>
                {deployment.customer_name || deployment.deployment_id}
              </option>
            ))}
          </select>
        </label>
        {selectedDeployment ? (
          <div className="userServerFacts">
            <StatusBadge tone={selectedDeployment.healthy ? "success" : "warning"}>
              {selectedDeployment.healthy ? "Healthy" : "Check server"}
            </StatusBadge>
            <span>{selectedDeployment.reported_version || selectedDeployment.current_version || "No version"}</span>
            <span>{selectedDeployment.user_management_v1 ? "User management ready" : "Upgrade required"}</span>
          </div>
        ) : <p>Choose a deployment before MC requests any user identity data.</p>}
      </section>

      {deploymentId && selectedDeployment && !selectedDeployment.user_management_v1 ? (
        <Notice tone="warning">Upgrade this server to a release that supports secure user management.</Notice>
      ) : null}

      {deploymentId && selectedDeployment?.user_management_v1 ? (
        <>
          <Panel
            eyebrow="Directory"
            title={selectedDeployment.customer_name || selectedDeployment.deployment_id}
            count={directory?.users.length ?? 0}
            actions={
              <div className="userDirectoryTools">
                <label><input checked={includeDeleted} type="checkbox" onChange={(event) => {
                  setIncludeDeleted(event.target.checked);
                  storeState(deploymentId, event.target.checked, readStoredState()?.job_id || "");
                }} /> Include deleted</label>
                <button disabled={busy} type="button" onClick={() => void loadDirectory(deploymentId, includeDeleted)}>
                  {busy ? "Waiting for server…" : directory ? "Refresh" : "Load users"}
                </button>
              </div>
            }
          >
            {pending && busy ? (
              <div className="userJobProgress" role="status">
                <span className="userJobPulse" />
                <strong>{pending.status === "queued" ? "Queued for the server" : "Server is applying the request"}</strong>
                <small>Job {pending.id}</small>
              </div>
            ) : null}
            {!directory && !busy ? (
              <div className="emptyState">Load the directory to manage accounts on this server.</div>
            ) : null}
            {directory ? (
              <div className="tableScroll userTableScroll">
                <table className="adminTable userTable">
                  <thead><tr><th>Person</th><th>Access</th><th>Status</th><th>Added</th><th>Actions</th></tr></thead>
                  <tbody>
                    {directory.users.map((user) => (
                      <tr key={user.id}>
                        <td><strong>{user.display_name}</strong><small>{user.email}</small></td>
                        <td><strong>{directory.roles.find((role) => role.id === user.role_id)?.label || user.role_id}</strong><small>{user.location || "No location"}</small></td>
                        <td><StatusBadge tone={statusTone(user.status)}>{user.status}</StatusBadge>{user.must_change_password ? <small>Must change password</small> : null}</td>
                        <td><Timestamp label="Added" value={user.created_at} /></td>
                        <td>
                          <div className="userRowActions">
                            {user.status !== "deleted" ? <button disabled={busy} type="button" onClick={() => {
                              if (window.confirm(`Reset ${user.email} to a one-time password and end every session?`)) {
                                void runMutation(
                                  () => resetManagedUserPassword(deploymentId, user.id),
                                  `Reset ${user.email}. Copy the one-time password now.`,
                                  `One-time password for ${user.email}`,
                                );
                              }
                            }}>Reset password</button> : null}
                            {user.status === "active" ? <button className="secondaryButton" disabled={busy} type="button" onClick={() => {
                              if (window.confirm(`Disable ${user.email} and end every session?`)) {
                                void runMutation(() => disableManagedUser(deploymentId, user.id), `Disabled ${user.email}.`);
                              }
                            }}>Disable</button> : null}
                            {user.status === "disabled" ? <button className="secondaryButton" disabled={busy} type="button" onClick={() => void runMutation(() => enableManagedUser(deploymentId, user.id), `Re-enabled ${user.email}.`)}>Re-enable</button> : null}
                            {user.status === "disabled" ? <button className="danger" disabled={busy} type="button" onClick={() => { setDeleteTarget(user); setDeleteConfirmation(""); }}>Delete</button> : null}
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : null}
          </Panel>

          {directory ? (
            <Panel eyebrow="New access" title="Create user">
              <form className="adminForm managedUserCreate" onSubmit={(event) => void onCreate(event)}>
                <label>Display name<input required maxLength={200} value={draft.display_name} onChange={(event) => setDraft({ ...draft, display_name: event.target.value })} /></label>
                <label>Email<input required type="email" value={draft.email} onChange={(event) => setDraft({ ...draft, email: event.target.value })} /></label>
                <label>Role<select required value={draft.role_id} onChange={(event) => setDraft({ ...draft, role_id: event.target.value, location: "" })}>{directory.roles.map((role) => <option key={role.id} value={role.id}>{role.label}</option>)}</select></label>
                <label>Location<select disabled={selectedRole?.scope !== "location"} required={selectedRole?.scope === "location"} value={selectedRole?.scope === "location" ? draft.location : "all"} onChange={(event) => setDraft({ ...draft, location: event.target.value })}><option value={selectedRole?.scope === "location" ? "" : "all"}>{selectedRole?.scope === "location" ? "Choose a location" : "All locations"}</option>{directory.locations.map((location) => <option key={location} value={location}>{location}</option>)}</select></label>
                <div className="managedUserCreateAction"><p>The server generates the initial password and forces a change at first sign-in.</p><button disabled={busy} type="submit">Create user</button></div>
              </form>
            </Panel>
          ) : null}

          {deleteTarget ? (
            <section className="userDeleteConfirm" aria-labelledby="userDeleteTitle">
              <div><p className="eyebrow">Permanent account action</p><h2 id="userDeleteTitle">Delete {deleteTarget.display_name}?</h2><p>Login identity is anonymized. Company content and audit history remain. Owned workspaces must be reassigned first.</p></div>
              <label>Type {deleteTarget.email} to confirm<input autoFocus value={deleteConfirmation} onChange={(event) => setDeleteConfirmation(event.target.value)} /></label>
              <div><button className="secondaryButton" type="button" onClick={() => setDeleteTarget(null)}>Cancel</button><button className="danger" disabled={deleteConfirmation !== deleteTarget.email || busy} type="button" onClick={() => void confirmDelete()}>Delete and anonymize</button></div>
            </section>
          ) : null}
        </>
      ) : null}
    </div>
  );
}
