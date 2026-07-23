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
  AccountingConfirmItemInput,
  AccountingDocument,
  AccountingLineItem,
  AccountingOverview,
  AccountingWorkspace,
} from "@/lib/onebrain-types";

// Only user-touched fields are held; an untouched line sends no correction so the
// backend uses its proposal (or re-proposes it when the direction is flipped).
type Edit = { account?: string; tax_key?: string };

// AccountingConfirmIn.confirmations is capped at 500 server-side.
const BATCH_LIMIT = 500;

function workspaceKey(workspace: AccountingWorkspace): string {
  return `${workspace.account_id}:${workspace.space_id}`;
}

function needsReview(document: AccountingDocument): boolean {
  return document.check_flags?.needs_review === true;
}

// A line with no proposed Steuerschlüssel (0%/unknown VAT) is not safe to batch-book
// blind — route it to single review even when the document-level flags look clean.
function hasUncertainBooking(document: AccountingDocument): boolean {
  return document.line_items.some((line) => !line.proposed_tax_key);
}

function money(value: string | null, currency: string): string {
  return value === null || value === "" ? "—" : `${value} ${currency}`;
}

function directionLabel(direction: string): string {
  return direction === "outgoing" ? "Outgoing" : "Incoming";
}

function lineDefault(line: AccountingLineItem, field: keyof Edit): string {
  return field === "account"
    ? line.confirmed_account || line.proposed_account
    : line.confirmed_tax_key || line.proposed_tax_key;
}

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
  if (flags.non_eur === true) reasons.push("Non-EUR — not in the EUR totals");
  return reasons;
}

// Phase 1: capture → review → book. Extraction drops pending drafts (via the Drive
// upload trigger); this panel is where a human confirms them. Clean drafts batch in
// one click; flagged ones are reviewed singly with direction + booking editable.
export function AccountingPanel() {
  const [workspaces, setWorkspaces] = useState<AccountingWorkspace[]>([]);
  const [selectedWorkspaceKey, setSelectedWorkspaceKey] = useState("");
  const [overview, setOverview] = useState<AccountingOverview | null>(null);
  const [pending, setPending] = useState<AccountingDocument[]>([]);
  const [edits, setEdits] = useState<Record<string, Edit>>({});
  const [directions, setDirections] = useState<Record<string, string>>({});
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
        // The review desk only needs pending drafts — never the whole ledger.
        const [nextOverview, nextPending] = await Promise.all([
          getAccountingOverview(selectedWorkspace!.account_id, selectedWorkspace!.space_id),
          listAccountingDocuments(selectedWorkspace!.account_id, selectedWorkspace!.space_id, "pending"),
        ]);
        if (cancelled) return;
        setOverview(nextOverview);
        setPending(nextPending);
        setEdits({});
        setDirections(Object.fromEntries(nextPending.map((document) => [document.id, document.direction])));
      } catch (loadError) {
        if (!cancelled) {
          setOverview(null);
          setPending([]);
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
  const canBook = selectedWorkspace?.can_configure ?? false;
  const cleanDocuments = useMemo(
    () => pending.filter((document) => !needsReview(document) && !hasUncertainBooking(document)),
    [pending],
  );
  const flaggedDocuments = useMemo(
    () => pending.filter((document) => needsReview(document) || hasUncertainBooking(document)),
    [pending],
  );

  function chooseWorkspace(key: string) {
    // Clear the previous workspace's drafts immediately so a stale confirm can't fire.
    setSelectedWorkspaceKey(key);
    setPending([]);
    setOverview(null);
    setEdits({});
    setDirections({});
    setNotice("");
  }

  async function confirm(confirmations: AccountingConfirmItemInput[]) {
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
    const item: AccountingConfirmItemInput = { document_id: document.id };
    const corrections = document.line_items
      .filter((line) => edits[line.id])
      .map((line) => ({ id: line.id, ...edits[line.id] }));
    if (corrections.length) item.line_items = corrections;
    const direction = directions[document.id];
    if (direction && direction !== document.direction) {
      item.direction = direction as "incoming" | "outgoing";
    }
    void confirm([item]);
  }

  function setEdit(lineId: string, field: keyof Edit, value: string) {
    setEdits((current) => ({ ...current, [lineId]: { ...current[lineId], [field]: value } }));
  }

  const actionsDisabled = busy || loadingData;
  const batch = cleanDocuments.slice(0, BATCH_LIMIT);

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
            {!canBook ? <StatusBadge tone="warning">read-only</StatusBadge> : null}
          </>
        ) : <StatusBadge tone="neutral">No workspace selected</StatusBadge>}
        actions={workspaces.length > 1 ? (
          <label className="compactField">
            <span>Workspace</span>
            <select
              disabled={loadingWorkspaces || busy}
              value={selectedWorkspaceKey}
              onChange={(event) => chooseWorkspace(event.target.value)}
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
      {selectedWorkspace && !canBook ? (
        <Notice tone="warning">You can review documents here, but booking needs the accounting configure permission.</Notice>
      ) : null}

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
          actions={canBook && cleanDocuments.length > 0 ? (
            <button
              className="primaryButton"
              type="button"
              disabled={actionsDisabled}
              onClick={() => confirm(batch.map((document) => ({ document_id: document.id })))}
            >
              {busy ? "Booking…" : `Confirm ${batch.length} clean${cleanDocuments.length > BATCH_LIMIT ? ` (of ${cleanDocuments.length})` : ""}`}
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
                    {canBook ? <th aria-label="actions" /> : null}
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
                      {canBook ? (
                        <td>
                          <button
                            className="textButton"
                            type="button"
                            disabled={actionsDisabled}
                            onClick={() => confirm([{ document_id: document.id }])}
                          >
                            Confirm
                          </button>
                        </td>
                      ) : null}
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
          intro="Drafts with a warning — check the reasons, set the direction/booking if needed, then confirm."
        >
          {!loadingData && flaggedDocuments.length === 0 ? (
            <p className="panelIntro">Nothing needs review.</p>
          ) : null}
          {flaggedDocuments.map((document) => (
            <ReviewCard
              key={document.id}
              document={document}
              edits={edits}
              direction={directions[document.id] ?? document.direction}
              canBook={canBook}
              busy={actionsDisabled}
              onEdit={setEdit}
              onDirection={(value) => setDirections((current) => ({ ...current, [document.id]: value }))}
              onConfirm={() => confirmReview(document)}
            />
          ))}
        </Panel>
      ) : null}
    </div>
  );
}

function ReviewCard({
  document,
  edits,
  direction,
  canBook,
  busy,
  onEdit,
  onDirection,
  onConfirm,
}: {
  document: AccountingDocument;
  edits: Record<string, Edit>;
  direction: string;
  canBook: boolean;
  busy: boolean;
  onEdit: (lineId: string, field: keyof Edit, value: string) => void;
  onDirection: (value: string) => void;
  onConfirm: () => void;
}) {
  const reasons = flagReasons(document.check_flags ?? {});
  return (
    <article className="reviewCard">
      <div className="pageHeaderMeta">
        <StatusBadge tone="neutral">{document.issuer_name || "Unknown issuer"}</StatusBadge>
        <StatusBadge tone="running">{document.invoice_number || "no number"}</StatusBadge>
        <StatusBadge tone="warning">{money(document.total_gross, document.currency)}</StatusBadge>
      </div>
      {reasons.length > 0 ? (
        <ul>
          {reasons.map((reason) => <li key={reason}>{reason}</li>)}
        </ul>
      ) : null}
      <label className="compactField">
        <span>Direction</span>
        <select value={direction} disabled={busy || !canBook} onChange={(event) => onDirection(event.target.value)}>
          <option value="incoming">Incoming (expense)</option>
          <option value="outgoing">Outgoing (revenue)</option>
        </select>
      </label>
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
                    value={edits[line.id]?.account ?? lineDefault(line, "account")}
                    disabled={busy || !canBook}
                    onChange={(event) => onEdit(line.id, "account", event.target.value)}
                    aria-label={`Account for ${line.description || `line ${line.line_no + 1}`}`}
                  />
                </td>
                <td>
                  <input
                    className="input"
                    value={edits[line.id]?.tax_key ?? lineDefault(line, "tax_key")}
                    disabled={busy || !canBook}
                    onChange={(event) => onEdit(line.id, "tax_key", event.target.value)}
                    aria-label={`Tax key for ${line.description || `line ${line.line_no + 1}`}`}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {canBook ? (
        <div className="panelActions">
          <button className="primaryButton" type="button" disabled={busy} onClick={onConfirm}>
            {busy ? "Booking…" : "Confirm booking"}
          </button>
        </div>
      ) : null}
    </article>
  );
}
