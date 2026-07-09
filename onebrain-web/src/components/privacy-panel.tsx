"use client";

import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import {
  erasePrivacyData,
  exportPrivacyData,
  listPlatformAccounts,
  listPlatformSpaces,
} from "@/lib/onebrain-client";
import { MetricStrip, Notice, PageHeader, Panel } from "@/components/admin-ui";
import type {
  PlatformAccount,
  PlatformSpace,
  PrivacyEraseResult,
  PrivacyExport,
} from "@/lib/onebrain-types";

type PrivacyResult =
  | { kind: "export"; exportData: PrivacyExport; chunks: number }
  | { kind: "erase"; eraseData: PrivacyEraseResult };

function labelFor(value: string): string {
  return value.replace(/_/g, " ");
}

function countExportChunks(documents: Array<Record<string, unknown>>): number {
  return documents.reduce((total, document) => {
    const chunks = document.chunks;
    if (Array.isArray(chunks)) {
      return total + chunks.length;
    }
    return total + (typeof chunks === "number" ? chunks : 0);
  }, 0);
}

function exportFilename(accountId: string, spaceId: string): string {
  const rawName = `onebrain-privacy-${accountId}${spaceId ? `-${spaceId}` : ""}.json`;
  return rawName.replace(/[^a-zA-Z0-9._-]/g, "_");
}

function downloadExport(payload: PrivacyExport) {
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = exportFilename(payload.account_id, payload.space_id);
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

export function PrivacyPanel() {
  const [accounts, setAccounts] = useState<PlatformAccount[]>([]);
  const [spaces, setSpaces] = useState<PlatformSpace[]>([]);
  const [selectedAccountId, setSelectedAccountId] = useState("");
  const [selectedSpaceId, setSelectedSpaceId] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [reason, setReason] = useState("");
  const [loadingAccounts, setLoadingAccounts] = useState(true);
  const [loadingSpaces, setLoadingSpaces] = useState(false);
  const [busyAction, setBusyAction] = useState<"export" | "erase" | "">("");
  const [showErase, setShowErase] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [result, setResult] = useState<PrivacyResult | null>(null);

  const selectedAccount = useMemo(
    () => accounts.find((account) => account.id === selectedAccountId) ?? null,
    [accounts, selectedAccountId],
  );
  const selectedSpace = useMemo(
    () => spaces.find((space) => space.id === selectedSpaceId) ?? null,
    [selectedSpaceId, spaces],
  );
  const eraseReady = Boolean(selectedAccountId && confirmation.trim() === selectedAccountId && !busyAction);

  const chooseAccount = useCallback((accountId: string) => {
    setSelectedAccountId(accountId);
    setSelectedSpaceId("");
    setSpaces([]);
    setLoadingSpaces(false);
    setConfirmation("");
    setReason("");
    setShowErase(false);
    setResult(null);
    setNotice("");
    setError("");
  }, []);

  const loadAccounts = useCallback(async () => {
    setLoadingAccounts(true);
    setError("");
    try {
      const nextAccounts = await listPlatformAccounts();
      setAccounts(nextAccounts);
      chooseAccount(
        selectedAccountId && nextAccounts.some((account) => account.id === selectedAccountId)
          ? selectedAccountId
          : nextAccounts[0]?.id ?? "",
      );
    } catch (err) {
      setAccounts([]);
      chooseAccount("");
      setSpaces([]);
      setError(err instanceof Error ? err.message : "Could not load accounts.");
    } finally {
      setLoadingAccounts(false);
    }
  }, [chooseAccount, selectedAccountId]);

  useEffect(() => {
    let active = true;
    void (async () => {
      setLoadingAccounts(true);
      setError("");
      try {
        const nextAccounts = await listPlatformAccounts();
        if (!active) {
          return;
        }
        setAccounts(nextAccounts);
        chooseAccount(nextAccounts[0]?.id ?? "");
      } catch (err) {
        if (!active) {
          return;
        }
        setAccounts([]);
        chooseAccount("");
        setError(err instanceof Error ? err.message : "Could not load accounts.");
      } finally {
        if (active) {
          setLoadingAccounts(false);
        }
      }
    })();
    return () => {
      active = false;
    };
  }, [chooseAccount]);

  useEffect(() => {
    let active = true;

    if (!selectedAccountId) {
      return () => {
        active = false;
      };
    }

    async function loadSpaces() {
      setLoadingSpaces(true);
      try {
        const nextSpaces = await listPlatformSpaces(selectedAccountId);
        if (active) {
          setSpaces(nextSpaces);
        }
      } catch (err) {
        if (active) {
          setSpaces([]);
          setError(err instanceof Error ? err.message : "Could not load spaces.");
        }
      } finally {
        if (active) {
          setLoadingSpaces(false);
        }
      }
    }

    void loadSpaces();

    return () => {
      active = false;
    };
  }, [selectedAccountId]);

  async function onExport() {
    if (!selectedAccountId || busyAction) {
      return;
    }
    setBusyAction("export");
    setError("");
    setNotice("");
    try {
      const exportData = await exportPrivacyData(selectedAccountId, selectedSpaceId);
      const chunks = countExportChunks(exportData.documents);
      downloadExport(exportData);
      setResult({ kind: "export", exportData, chunks });
      setNotice("Export downloaded.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Export failed.");
    } finally {
      setBusyAction("");
    }
  }

  async function onErase(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!eraseReady) {
      return;
    }
    setBusyAction("erase");
    setError("");
    setNotice("");
    try {
      const eraseData = await erasePrivacyData(selectedAccountId, {
        confirm_account_id: confirmation.trim(),
        reason: reason.trim(),
        space_id: selectedSpaceId,
      });
      setResult({ kind: "erase", eraseData });
      setNotice("Data erased and audit event recorded.");
      setConfirmation("");
      setReason("");
      setShowErase(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Erase failed.");
    } finally {
      setBusyAction("");
    }
  }

  return (
    <div className="privacyWorkspace">
      <PageHeader
        actions={(
          <>
            <button className="secondaryButton" disabled={loadingAccounts || Boolean(busyAction)} type="button" onClick={() => void loadAccounts()}>
              {loadingAccounts ? "Loading" : "Refresh"}
            </button>
            <button className="primaryButton" disabled={!selectedAccountId || Boolean(busyAction)} type="button" onClick={() => void onExport()}>
              {busyAction === "export" ? "Exporting" : "Export JSON"}
            </button>
          </>
        )}
        eyebrow="Data rights"
        meta={selectedAccount ? <span className="scopePill"><span className="statusDot" />{selectedAccount.name}</span> : null}
        title="Privacy"
      />

      {error ? <Notice tone="error">{error}</Notice> : null}
      {notice ? <Notice tone="success">{notice}</Notice> : null}

      <MetricStrip
        metrics={[
          { label: "accounts", value: accounts.length },
          { label: "spaces", value: spaces.length },
          { label: "scope", value: selectedSpaceId ? "Space" : "Account" },
          { label: "status", tone: busyAction ? "warning" : "success", value: busyAction ? "Working" : "Ready" },
        ]}
      />

      <div className="privacyGrid">
        <Panel count={selectedAccount?.status || "none"} eyebrow="Scope" title="Data boundary">
          <label className="field">
            <span className="fieldLabel">Account</span>
            <select
              className="select"
              disabled={loadingAccounts || !accounts.length || Boolean(busyAction)}
              value={selectedAccountId}
              onChange={(event) => chooseAccount(event.target.value)}
            >
              {accounts.length === 0 ? <option value="">No accounts available</option> : null}
              {accounts.map((account) => (
                <option key={account.id} value={account.id}>
                  {account.name} ({account.id})
                </option>
              ))}
            </select>
          </label>

          <label className="field">
            <span className="fieldLabel">Space</span>
            <select
              className="select"
              disabled={!selectedAccountId || loadingSpaces || Boolean(busyAction)}
              value={selectedSpaceId}
              onChange={(event) => setSelectedSpaceId(event.target.value)}
            >
              <option value="">All account data</option>
              {spaces.map((space) => (
                <option key={space.id} value={space.id}>
                  {space.name} ({labelFor(space.kind)})
                </option>
              ))}
            </select>
          </label>

          <div className="privacyScopeCard">
            <span className="statusDot" aria-hidden="true" />
            <div>
              <strong>{selectedAccount?.name || "No account selected"}</strong>
              <p>{selectedSpace ? selectedSpace.name : "All account data"}</p>
            </div>
          </div>

        </Panel>

        {showErase ? (
          <section className="adminPanel privacyDanger" aria-labelledby="privacyEraseTitle">
            <div className="panelHead">
              <div>
                <p className="eyebrow">Erasure</p>
                <h2 id="privacyEraseTitle">Delete selected data</h2>
              </div>
              <button className="secondaryButton" type="button" onClick={() => setShowErase(false)}>Close</button>
            </div>
            <form className="privacyForm" onSubmit={(event) => void onErase(event)}>
              <div className="privacyScopeCard">
                <span className="statusDot" aria-hidden="true" />
                <div>
                  <strong>{selectedAccount?.name || "No account selected"}</strong>
                  <p>{selectedSpace ? selectedSpace.name : "All account data"}</p>
                </div>
              </div>

              <label className="field">
                <span className="fieldLabel">Confirm account id</span>
                <input
                  className="input"
                  disabled={!selectedAccountId || Boolean(busyAction)}
                  placeholder={selectedAccountId || "account id"}
                  value={confirmation}
                  onChange={(event) => setConfirmation(event.target.value)}
                />
              </label>

              <label className="field">
                <span className="fieldLabel">Reason</span>
                <textarea
                  className="textarea"
                  disabled={!selectedAccountId || Boolean(busyAction)}
                  maxLength={500}
                  rows={4}
                  value={reason}
                  onChange={(event) => setReason(event.target.value)}
                />
              </label>

              <p className="privacyNote">
                This removes the selected scope from documents, chunks, conversations, and intake records, then records
                the action in the Python audit store.
              </p>

              <button className="dangerButton" disabled={!eraseReady} type="submit">
                {busyAction === "erase" ? "Erasing" : "Erase data"}
              </button>
            </form>
          </section>
        ) : (
          <section className="adminPanel privacyStandby" aria-label="Erasure standby">
            <div>
              <p className="eyebrow">Erasure</p>
              <h2>Protected by confirmation</h2>
              <p className="operatorMuted">Choose a scope, then prepare erasure when a verified deletion request exists.</p>
            </div>
            <button className="secondaryButton" disabled={!selectedAccountId || Boolean(busyAction)} type="button" onClick={() => setShowErase(true)}>
              Prepare erasure
            </button>
          </section>
        )}
      </div>

      {result ? <PrivacyResultPanel result={result} /> : null}
    </div>
  );
}

function SummaryStat({ label, value }: { label: string; value: number | string }) {
  return (
    <div>
      <strong>{value}</strong>
      <span>{label}</span>
    </div>
  );
}

function PrivacyResultPanel({ result }: { result: PrivacyResult }) {
  if (result.kind === "erase") {
    const eraseData = result.eraseData;
    const governanceDeleted = Object.values(eraseData.governance_deleted || {}).reduce((total, value) => total + value, 0);
    return (
      <section className="privacyPanel resultPanel" aria-labelledby="privacyResultTitle">
        <div className="panelHead">
          <div>
            <p className="eyebrow">Last action</p>
            <h2 id="privacyResultTitle">Erase completed</h2>
          </div>
          <span>{eraseData.space_id ? "space" : "account"}</span>
        </div>
        <div className="resultGrid">
          <SummaryStat label="documents" value={eraseData.documents_deleted} />
          <SummaryStat label="chunks" value={eraseData.chunks_deleted} />
          <SummaryStat label="conversations" value={eraseData.conversations_deleted} />
          <SummaryStat label="intake" value={eraseData.intake_records_deleted} />
          <SummaryStat label="governance" value={governanceDeleted} />
        </div>
        <p className="auditLine">Audit event: {eraseData.audit_event_id}</p>
      </section>
    );
  }

  const exportData = result.exportData;
  const governanceRecords = Object.values(exportData.governance || {}).reduce(
    (total, records) => total + records.length,
    0,
  );
  return (
    <section className="privacyPanel resultPanel" aria-labelledby="privacyResultTitle">
      <div className="panelHead">
        <div>
          <p className="eyebrow">Last action</p>
          <h2 id="privacyResultTitle">Export downloaded</h2>
        </div>
        <span>{exportData.space_id ? "space" : "account"}</span>
      </div>
      <div className="resultGrid">
        <SummaryStat label="documents" value={exportData.documents.length} />
        <SummaryStat label="chunks" value={result.chunks} />
        <SummaryStat label="conversations" value={exportData.conversations.length} />
        <SummaryStat label="intake" value={exportData.intake_records.length} />
        <SummaryStat label="governance" value={governanceRecords} />
        <SummaryStat label="audit events" value={exportData.audit_events.length} />
      </div>
      <p className="auditLine">Exported at: {exportData.exported_at}</p>
    </section>
  );
}
