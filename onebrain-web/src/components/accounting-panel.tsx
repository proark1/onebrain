"use client";

import { useEffect, useMemo, useState } from "react";
import { MetricStrip, Notice, PageHeader, Panel, StatusBadge } from "@/components/admin-ui";
import {
  confirmAccountingDocuments,
  getAccountingOverview,
  listAccountingDocuments,
  listAccountingWorkspaces,
} from "@/lib/onebrain-client";
import type {
  AccountingDocument,
  AccountingLineItem,
  AccountingOverview,
  AccountingWorkspace,
} from "@/lib/onebrain-types";

type Edit = { account: string; tax_key: string };

function workspaceKey(workspace: AccountingWorkspace): string {
  return `${workspace.account_id}:${workspace.space_id}`;
}

function needsReview(document: AccountingDocument): boolean {
  return document.check_flags?.needs_review === true;
}

function money(value: string | null, currency: string): string {
  return value === null || value === "" ? "—" : `${value} ${currency}`;
}

function directionLabel(direction: string): string {
  return direction === "outgoing" ? "Outgoing" : "Incoming";
}

// Turn the raw check_flags into short human reasons a reviewer can act on.
function flagReasons(flags: Record<string, unknown>): string[] {
  const reasons: string[] = [];
  if (flags.arithmetic_ok === false) reasons.push("Net + VAT ≠ gross");
  if (flags.mandatory_complete === false) {
    const missing = Array.isArray(flags.missing_fields) ? flags.missing_fields.join(", ") : "";
    reasons.push(missing ? `Missing §14 fields: ${missing}` : "Missing §14 fields");
  }
  if (flags.vat_id_valid === false) reasons.push("USt-IdNr looks malformed");
  if (flags.duplicate === true) reasons.push("Possible duplicate");
  if (flags.invoice_number_unique === false) reasons.push("Invoice number seen before");
  if (flags.reverse_charge === true) reasons.push("Reverse charge (§13b)");
  if (flags.intra_community === true) reasons.push("Intra-community");
  return reasons;
}

function initialEdits(documents: AccountingDocument[]): Record<string, Edit> {
  const edits: Record<string, Edit> = {};
  for (const document of documents) {
    for (const line of document.line_items) {
      edits[line.id] = {
        account: line.confirmed_account || line.proposed_account,
        tax_key: line.confirmed_tax_key || line.proposed_tax_key,
      };
    }
  }
  return edits;
}

// Phase 1: capture → review → book. Extraction drops pending drafts (via the Drive
// upload trigger); this panel is where a human confirms them. Clean drafts batch in
// one click; flagged ones are reviewed singly with their booking editable.
export function AccountingPanel() {
  const [workspaces, setWorkspaces] = useState<AccountingWorkspace[]>([]);
  const [selectedWorkspaceKey, setSelectedWorkspaceKey] = useState("");
  const [overview, setOverview] = useState<AccountingOverview | null>(null);
  const [documents, setDocuments] = useState<AccountingDocument[]>([]);
  const [edits, setEdits] = useState<Record<string, Edit>>({});
  const [loadingWorkspaces, setLoadingWorkspaces] = useState(true);
  const [loadingData, setLoadingData] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [refreshVersion, setRefreshVersion] = useState(0);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoadingWorkspaces(true);
      setError("");
      try {
        const rows = await listAccountingWorkspaces();
        if (cancelled) return;
        setWorkspaces(rows);
        setSelectedWorkspaceKey((current) => (rows.some((row) => workspaceKey(row) === current)
          ? current
          : rows[0] ? workspaceKey(rows[0]) : ""));
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
      setLoadingData(true);
      setError("");
      try {
        const [nextOverview, nextDocuments] = await Promise.all([
          getAccountingOverview(selectedWorkspace!.account_id, selectedWorkspace!.space_id),
          listAccountingDocuments(selectedWorkspace!.account_id, selectedWorkspace!.space_id),
        ]);
        if (cancelled) return;
        setOverview(nextOverview);
        setDocuments(nextDocuments);
        setEdits(initialEdits(nextDocuments));
      } catch (loadError) {
        if (!cancelled) {
          setOverview(null);
          setDocuments([]);
          setError(loadError instanceof Error ? loadError.message : "Could not load the accounting overview.");
        }
      } finally {
        if (!cancelled) setLoadingData(false);
      }
    }
    void load();
    return () => { cancelled = true; };
  }, [selectedWorkspace, refreshVersion]);

  const view = selectedWorkspace ? overview : null;
  const pending = useMemo(() => documents.filter((document) => document.status === "pending"), [documents]);
  const cleanDocuments = useMemo(() => pending.filter((document) => !needsReview(document)), [pending]);
  const flaggedDocuments = useMemo(() => pending.filter(needsReview), [pending]);
  const bookedDocuments = useMemo(
    () => documents.filter((document) => document.status === "confirmed").slice(0, 8),
    [documents],
  );

  async function confirm(confirmations: { document_id: string; line_items?: { id: string; account: string; tax_key: string }[] }[]) {
    if (!selectedWorkspace || confirmations.length === 0) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await confirmAccountingDocuments({
        account_id: selectedWorkspace.account_id,
        space_id: selectedWorkspace.space_id,
        confirmations,
      });
      setNotice(confirmations.length === 1 ? "Document booked." : `${confirmations.length} documents booked.`);
      setRefreshVersion((version) => version + 1);
    } catch (confirmError) {
      setError(confirmError instanceof Error ? confirmError.message : "Could not confirm the document(s).");
    } finally {
      setBusy(false);
    }
  }

  function confirmReview(document: AccountingDocument) {
    const corrections = document.line_items.map((line) => ({
      id: line.id,
      account: edits[line.id]?.account ?? line.proposed_account,
      tax_key: edits[line.id]?.tax_key ?? line.proposed_tax_key,
    }));
    void confirm([{ document_id: document.id, line_items: corrections }]);
  }

  function setEdit(lineId: string, field: keyof Edit, value: string) {
    setEdits((current) => ({ ...current, [lineId]: { ...current[lineId], [field]: value } }));
  }

  return (
    <div className="accountingWorkspace">
      <PageHeader
        description="Invoices captured through Drive are extracted, checked, and pre-booked. Review the drafts here — confirm clean ones as a batch, and correct the flagged ones — to book them."
        eyebrow="Buchhaltung"
        title="Accounting"
        meta={selectedWorkspace ? (
          <>
            <StatusBadge tone="neutral">{selectedWorkspace.account_name}</StatusBadge>
            <StatusBadge tone="running">{selectedWorkspace.space_name}</StatusBadge>
          </>
        ) : <StatusBadge tone="neutral">No workspace selected</StatusBadge>}
        actions={workspaces.length > 1 ? (
          <label className="compactField">
            <span>Workspace</span>
            <select
              disabled={loadingWorkspaces || busy}
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
      {notice ? <Notice tone="success">{notice}</Notice> : null}

      <MetricStrip
        metrics={[
          { label: "documents", value: view?.total_documents ?? 0 },
          {
            label: "pending review",
            tone: (view?.pending_documents ?? 0) ? "warning" : undefined,
            value: view?.pending_documents ?? 0,
          },
          { label: "booked", tone: "success", value: view?.confirmed_documents ?? 0 },
          { label: "Vorsteuer (input VAT)", value: money(view?.input_vat ?? null, view?.currency ?? "EUR") },
          { label: "Umsatzsteuer (output VAT)", value: money(view?.output_vat ?? null, view?.currency ?? "EUR") },
          { label: "USt balance", value: money(view?.vat_balance ?? null, view?.currency ?? "EUR") },
        ]}
      />

      {loadingWorkspaces ? (
        <Panel eyebrow="Overview" title="Documents"><p>Loading accounting workspaces…</p></Panel>
      ) : null}

      {!loadingWorkspaces && !selectedWorkspace && !error ? (
        <Panel eyebrow="Overview" title="Documents">
          <div className="emptyState">
            <span className="emptyMark">₀</span>
            <h2>Accounting is not enabled</h2>
            <p>
              Install the Accounting module for a space and allow the <code>accounting_read</code>{" "}
              purpose to review invoices here.
            </p>
          </div>
        </Panel>
      ) : null}

      {selectedWorkspace ? (
        <Panel
          eyebrow="Review by exception"
          title="Ready to book"
          count={cleanDocuments.length}
          intro="Clean, non-duplicate drafts that passed every check. Book them in one click."
          actions={cleanDocuments.length > 0 ? (
            <button
              className="primaryButton"
              type="button"
              disabled={busy}
              onClick={() => confirm(cleanDocuments.map((document) => ({ document_id: document.id })))}
            >
              {busy ? "Booking…" : `Confirm all (${cleanDocuments.length})`}
            </button>
          ) : null}
        >
          {loadingData && pending.length === 0 ? <p>Loading documents…</p> : null}
          {!loadingData && cleanDocuments.length === 0 ? (
            <p className="panelIntro">Nothing ready to book. Flagged drafts, if any, are below.</p>
          ) : null}
          {cleanDocuments.length > 0 ? (
            <div className="tableScroll">
              <table className="adminTable">
                <thead>
                  <tr>
                    <th>Issuer</th>
                    <th>Invoice</th>
                    <th>Date</th>
                    <th>Direction</th>
                    <th>Gross</th>
                    <th aria-label="actions" />
                  </tr>
                </thead>
                <tbody>
                  {cleanDocuments.map((document) => (
                    <tr key={document.id}>
                      <td>{document.issuer_name || "—"}</td>
                      <td>{document.invoice_number || "—"}</td>
                      <td>{document.invoice_date ?? "—"}</td>
                      <td>{directionLabel(document.direction)}</td>
                      <td>{money(document.total_gross, document.currency)}</td>
                      <td>
                        <button
                          className="textButton"
                          type="button"
                          disabled={busy}
                          onClick={() => confirm([{ document_id: document.id }])}
                        >
                          Confirm
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
        </Panel>
      ) : null}

      {selectedWorkspace ? (
        <Panel
          eyebrow="Review by exception"
          title="Needs review"
          count={flaggedDocuments.length}
          intro="Drafts with a warning — check the reasons, correct the booking if needed, then confirm."
        >
          {!loadingData && flaggedDocuments.length === 0 ? (
            <p className="panelIntro">Nothing needs review.</p>
          ) : null}
          {flaggedDocuments.map((document) => (
            <ReviewCard
              key={document.id}
              document={document}
              edits={edits}
              busy={busy}
              onEdit={setEdit}
              onConfirm={() => confirmReview(document)}
            />
          ))}
        </Panel>
      ) : null}

      {selectedWorkspace && bookedDocuments.length > 0 ? (
        <Panel eyebrow="Booked" title="Recently booked" count={view?.confirmed_documents ?? bookedDocuments.length}>
          <div className="tableScroll">
            <table className="adminTable">
              <thead>
                <tr>
                  <th>Issuer</th>
                  <th>Invoice</th>
                  <th>Direction</th>
                  <th>Gross</th>
                </tr>
              </thead>
              <tbody>
                {bookedDocuments.map((document) => (
                  <tr key={document.id}>
                    <td>{document.issuer_name || "—"}</td>
                    <td>{document.invoice_number || "—"}</td>
                    <td>{directionLabel(document.direction)}</td>
                    <td>{money(document.total_gross, document.currency)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      ) : null}
    </div>
  );
}

function ReviewCard({
  document,
  edits,
  busy,
  onEdit,
  onConfirm,
}: {
  document: AccountingDocument;
  edits: Record<string, Edit>;
  busy: boolean;
  onEdit: (lineId: string, field: keyof Edit, value: string) => void;
  onConfirm: () => void;
}) {
  const reasons = flagReasons(document.check_flags ?? {});
  return (
    <article className="reviewCard">
      <div className="pageHeaderMeta">
        <StatusBadge tone="neutral">{document.issuer_name || "Unknown issuer"}</StatusBadge>
        <StatusBadge tone="running">{document.invoice_number || "no number"}</StatusBadge>
        <StatusBadge tone="neutral">{directionLabel(document.direction)}</StatusBadge>
        <StatusBadge tone="warning">{money(document.total_gross, document.currency)}</StatusBadge>
      </div>
      {reasons.length > 0 ? (
        <ul>
          {reasons.map((reason) => <li key={reason}>{reason}</li>)}
        </ul>
      ) : null}
      <div className="tableScroll">
        <table className="adminTable">
          <thead>
            <tr>
              <th>Position</th>
              <th>Net</th>
              <th>Rate</th>
              <th>Account (SKR03)</th>
              <th>Steuerschlüssel</th>
            </tr>
          </thead>
          <tbody>
            {document.line_items.map((line: AccountingLineItem) => (
              <tr key={line.id}>
                <td>{line.description || `Line ${line.line_no + 1}`}</td>
                <td>{money(line.amount_net, document.currency)}</td>
                <td>{line.tax_rate ?? "—"}</td>
                <td>
                  <input
                    className="input"
                    value={edits[line.id]?.account ?? line.proposed_account}
                    onChange={(event) => onEdit(line.id, "account", event.target.value)}
                    aria-label={`Account for ${line.description || `line ${line.line_no + 1}`}`}
                  />
                </td>
                <td>
                  <input
                    className="input"
                    value={edits[line.id]?.tax_key ?? line.proposed_tax_key}
                    onChange={(event) => onEdit(line.id, "tax_key", event.target.value)}
                    aria-label={`Tax key for ${line.description || `line ${line.line_no + 1}`}`}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="panelActions">
        <button className="primaryButton" type="button" disabled={busy} onClick={onConfirm}>
          {busy ? "Booking…" : "Confirm booking"}
        </button>
      </div>
    </article>
  );
}
