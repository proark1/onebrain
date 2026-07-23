"use client";

import { useEffect, useMemo, useState } from "react";
import { MetricStrip, Notice, PageHeader, Panel, StatusBadge } from "@/components/admin-ui";
import { getAccountingOverview, listAccountingWorkspaces } from "@/lib/onebrain-client";
import type { AccountingOverview, AccountingWorkspace } from "@/lib/onebrain-types";

function workspaceKey(workspace: AccountingWorkspace): string {
  return `${workspace.account_id}:${workspace.space_id}`;
}

// Phase 0 skeleton: this panel proves the Accounting module can be switched on
// per workspace (403 when it is not) and reads the still-empty overview. The
// capture, extraction, booking-proposal, and query surfaces arrive in later phases.
export function AccountingPanel() {
  const [workspaces, setWorkspaces] = useState<AccountingWorkspace[]>([]);
  const [selectedWorkspaceKey, setSelectedWorkspaceKey] = useState("");
  const [overview, setOverview] = useState<AccountingOverview | null>(null);
  const [loadingWorkspaces, setLoadingWorkspaces] = useState(true);
  const [loadingOverview, setLoadingOverview] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoadingWorkspaces(true);
      setError("");
      try {
        const rows = await listAccountingWorkspaces();
        if (cancelled) return;
        setWorkspaces(rows);
        setSelectedWorkspaceKey((current) => rows.some((row) => workspaceKey(row) === current)
          ? current
          : rows[0] ? workspaceKey(rows[0]) : "");
      } catch (loadError) {
        if (!cancelled) setError(loadError instanceof Error ? loadError.message : "Could not load accounting workspaces.");
      } finally {
        if (!cancelled) setLoadingWorkspaces(false);
      }
    }
    void load();
    return () => { cancelled = true; };
  }, []);

  const selectedWorkspace = useMemo(
    () => workspaces.find((workspace) => workspaceKey(workspace) === selectedWorkspaceKey) ?? null,
    [selectedWorkspaceKey, workspaces],
  );

  useEffect(() => {
    if (!selectedWorkspace) return;
    let cancelled = false;
    async function load() {
      setLoadingOverview(true);
      setError("");
      try {
        const next = await getAccountingOverview(selectedWorkspace!.account_id, selectedWorkspace!.space_id);
        if (!cancelled) setOverview(next);
      } catch (loadError) {
        if (!cancelled) {
          setOverview(null);
          setError(loadError instanceof Error ? loadError.message : "Could not load the accounting overview.");
        }
      } finally {
        if (!cancelled) setLoadingOverview(false);
      }
    }
    void load();
    return () => { cancelled = true; };
  }, [selectedWorkspace]);

  // Never show a stale overview once the selection clears.
  const overviewForWorkspace = selectedWorkspace ? overview : null;

  return (
    <div className="accountingWorkspace">
      <PageHeader
        description="Capture, review, and book invoices. Phase 0 proves the module can be switched on per workspace; the extraction and booking surfaces arrive next."
        eyebrow="Buchhaltung"
        title="Accounting"
        meta={selectedWorkspace ? (
          <>
            <StatusBadge tone="neutral">{selectedWorkspace.account_name}</StatusBadge>
            <StatusBadge tone="running">{selectedWorkspace.space_name}</StatusBadge>
            <StatusBadge tone="warning">preview</StatusBadge>
          </>
        ) : <StatusBadge tone="neutral">No workspace selected</StatusBadge>}
        actions={workspaces.length > 1 ? (
          <label className="compactField">
            <span>Workspace</span>
            <select
              disabled={loadingWorkspaces}
              value={selectedWorkspaceKey}
              onChange={(event) => setSelectedWorkspaceKey(event.target.value)}
            >
              {workspaces.map((workspace) => (
                <option key={workspaceKey(workspace)} value={workspaceKey(workspace)}>
                  {workspace.account_name} / {workspace.space_name}
                </option>
              ))}
            </select>
          </label>
        ) : null}
      />

      {error ? <Notice tone="error">{error}</Notice> : null}

      <MetricStrip
        metrics={[
          { label: "documents", value: overviewForWorkspace?.total_documents ?? 0 },
          {
            label: "pending review",
            tone: (overviewForWorkspace?.pending_documents ?? 0) ? "warning" : undefined,
            value: overviewForWorkspace?.pending_documents ?? 0,
          },
          { label: "booked", tone: "success", value: overviewForWorkspace?.confirmed_documents ?? 0 },
        ]}
      />

      <Panel eyebrow="Overview" title="Documents">
        {loadingWorkspaces ? <p>Loading accounting workspaces…</p> : null}

        {!loadingWorkspaces && !selectedWorkspace && !error ? (
          <p>
            Accounting is not enabled for a space you can access. Install the Accounting module
            for a space and allow the <code>accounting_read</code> purpose.
          </p>
        ) : null}

        {!loadingOverview && overviewForWorkspace && overviewForWorkspace.total_documents === 0 ? (
          <p>
            No documents yet. Invoice capture, extraction, and booking proposals arrive in the next
            phases — the module frame is live and switched on for this workspace.
          </p>
        ) : null}
      </Panel>
    </div>
  );
}
