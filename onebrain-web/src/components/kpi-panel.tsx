"use client";

import type { FormEvent, InputHTMLAttributes } from "react";
import { useEffect, useMemo, useState } from "react";
import { MetricStrip, Notice, PageHeader, Panel, StatusBadge } from "@/components/admin-ui";
import {
  createKpiDefinition,
  createManualKpiSnapshot,
  getKpiDashboard,
  listKpiWorkspaces,
  updateKpiDefinition,
} from "@/lib/onebrain-client";
import type {
  CreateKpiDefinitionInput,
  KpiDashboard,
  KpiDashboardItem,
  KpiDefinition,
  KpiWorkspace,
} from "@/lib/onebrain-types";

type KpiDraft = {
  key: string;
  name: string;
  description: string;
  category: string;
  unit: string;
  source_label: string;
  owner_label: string;
  freshness_minutes: string;
  warning_min: string;
  warning_max: string;
  critical_min: string;
  critical_max: string;
  display_order: string;
};

const EMPTY_DRAFT: KpiDraft = {
  key: "",
  name: "",
  description: "",
  category: "",
  unit: "",
  source_label: "",
  owner_label: "",
  freshness_minutes: "1440",
  warning_min: "",
  warning_max: "",
  critical_min: "",
  critical_max: "",
  display_order: "0",
};

const NUMBER_FORMAT = new Intl.NumberFormat(undefined, { maximumFractionDigits: 4 });
const PERCENT_FORMAT = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });

function workspaceKey(workspace: KpiWorkspace): string {
  return `${workspace.account_id}:${workspace.space_id}`;
}

function localDateTimeValue(date = new Date()): string {
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

function formatNumber(value: string | null): string {
  if (value === null) return "—";
  const number = Number(value);
  return Number.isFinite(number) ? NUMBER_FORMAT.format(number) : "—";
}

function formatValue(value: string | null, unit: string): string {
  const formatted = formatNumber(value);
  if (formatted === "—" || !unit) return formatted;
  return unit === "%" ? `${formatted}%` : `${formatted} ${unit}`;
}

function formatDelta(item: KpiDashboardItem): string {
  if (item.absolute_delta === null) return "No comparison";
  const absolute = Number(item.absolute_delta);
  const percentage = item.percentage_delta === null ? null : Number(item.percentage_delta);
  const sign = absolute > 0 ? "+" : "";
  const value = Number.isFinite(absolute) ? `${sign}${NUMBER_FORMAT.format(absolute)}` : "—";
  if (percentage === null || !Number.isFinite(percentage)) return value;
  const percentSign = percentage > 0 ? "+" : "";
  return `${value} · ${percentSign}${PERCENT_FORMAT.format(percentage)}%`;
}

function formatObservedAt(value: string | undefined): string {
  if (!value) return "Awaiting first observation";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Observation time unavailable";
  const elapsedMinutes = Math.max(0, Math.round((Date.now() - date.getTime()) / 60_000));
  if (elapsedMinutes < 1) return "Observed just now";
  if (elapsedMinutes < 60) return `Observed ${elapsedMinutes}m ago`;
  if (elapsedMinutes < 1_440) return `Observed ${Math.round(elapsedMinutes / 60)}h ago`;
  return `Observed ${Math.round(elapsedMinutes / 1_440)}d ago`;
}

function definitionDraft(definition: KpiDefinition): KpiDraft {
  return {
    key: definition.key,
    name: definition.name,
    description: definition.description,
    category: definition.category,
    unit: definition.unit,
    source_label: definition.source_label,
    owner_label: definition.owner_label,
    freshness_minutes: String(definition.freshness_minutes),
    warning_min: definition.warning_min ?? "",
    warning_max: definition.warning_max ?? "",
    critical_min: definition.critical_min ?? "",
    critical_max: definition.critical_max ?? "",
    display_order: String(definition.display_order),
  };
}

function thresholdTone(state: KpiDashboardItem["threshold_state"]): "danger" | "neutral" | "success" | "warning" {
  if (state === "critical") return "danger";
  if (state === "warning") return "warning";
  if (state === "healthy") return "success";
  return "neutral";
}

function ObservationHorizon({ item }: { item: KpiDashboardItem }) {
  const values = item.history.map((point) => Number(point.value)).filter(Number.isFinite);
  if (values.length === 0) {
    return <div className="kpiHorizonEmpty" aria-label="No observation history">No history</div>;
  }
  const low = Math.min(...values);
  const high = Math.max(...values);
  const range = high - low || 1;
  const points = values.map((value, index) => {
    const x = values.length === 1 ? 90 : 4 + (index / (values.length - 1)) * 172;
    const y = 40 - ((value - low) / range) * 32;
    return `${x},${y}`;
  }).join(" ");
  const [lastX, lastY] = points.split(" ").at(-1)?.split(",").map(Number) ?? [90, 24];
  return (
    <svg className="kpiHorizon" viewBox="0 0 180 48" role="img" aria-label={`${item.definition.name} observation history`}>
      <line x1="4" x2="176" y1="40" y2="40" />
      {values.length > 1 ? <polyline points={points} /> : null}
      <circle cx={lastX} cy={lastY} r="3.5" />
    </svg>
  );
}

function DraftField({
  label,
  name,
  value,
  onChange,
  ...inputProps
}: {
  label: string;
  name: keyof KpiDraft;
  value: string;
  onChange: (name: keyof KpiDraft, value: string) => void;
} & Omit<InputHTMLAttributes<HTMLInputElement>, "name" | "onChange" | "value">) {
  return (
    <label className="field">
      <span className="fieldLabel">{label}</span>
      <input
        {...inputProps}
        className="input"
        name={name}
        value={value}
        onChange={(event) => onChange(name, event.target.value)}
      />
    </label>
  );
}

export function KpiPanel() {
  const [workspaces, setWorkspaces] = useState<KpiWorkspace[]>([]);
  const [selectedWorkspaceKey, setSelectedWorkspaceKey] = useState("");
  const [dashboard, setDashboard] = useState<KpiDashboard | null>(null);
  const [includeArchived, setIncludeArchived] = useState(false);
  const [refreshVersion, setRefreshVersion] = useState(0);
  const [loadingWorkspaces, setLoadingWorkspaces] = useState(true);
  const [loadingDashboard, setLoadingDashboard] = useState(false);
  const [busyAction, setBusyAction] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState<KpiDraft>(EMPTY_DRAFT);
  const [showEditor, setShowEditor] = useState(false);
  const [manualDefinition, setManualDefinition] = useState<KpiDefinition | null>(null);
  const [manualValue, setManualValue] = useState("");
  const [manualObservedAt, setManualObservedAt] = useState(() => localDateTimeValue());

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoadingWorkspaces(true);
      setError("");
      try {
        const rows = await listKpiWorkspaces();
        if (cancelled) return;
        setWorkspaces(rows);
        setSelectedWorkspaceKey((current) => rows.some((row) => workspaceKey(row) === current)
          ? current
          : rows[0] ? workspaceKey(rows[0]) : "");
      } catch (loadError) {
        if (!cancelled) setError(loadError instanceof Error ? loadError.message : "Could not load KPI workspaces.");
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
      setLoadingDashboard(true);
      setError("");
      try {
        const next = await getKpiDashboard(
          selectedWorkspace!.account_id,
          selectedWorkspace!.space_id,
          30,
          includeArchived,
        );
        if (!cancelled) setDashboard(next);
      } catch (loadError) {
        if (!cancelled) {
          setDashboard(null);
          setError(loadError instanceof Error ? loadError.message : "Could not load KPI observations.");
        }
      } finally {
        if (!cancelled) setLoadingDashboard(false);
      }
    }
    void load();
    return () => { cancelled = true; };
  }, [includeArchived, refreshVersion, selectedWorkspace]);

  const activeItems = useMemo(
    () => dashboard?.items.filter((item) => item.definition.status === "active") ?? [],
    [dashboard],
  );
  const summary = useMemo(() => ({
    active: activeItems.length,
    healthy: activeItems.filter((item) => item.threshold_state === "healthy" && item.freshness_state === "fresh").length,
    attention: activeItems.filter((item) => item.threshold_state === "warning" || item.freshness_state === "stale").length,
    critical: activeItems.filter((item) => item.threshold_state === "critical").length,
    awaiting: activeItems.filter((item) => item.threshold_state === "awaiting_data").length,
  }), [activeItems]);

  function chooseWorkspace(key: string) {
    setSelectedWorkspaceKey(key);
    setDashboard(null);
    setIncludeArchived(false);
    closeEditor();
    closeManualEntry();
    setNotice("");
  }

  function openCreate() {
    setEditingId(null);
    setDraft(EMPTY_DRAFT);
    setShowEditor(true);
    setManualDefinition(null);
    setError("");
  }

  function openEdit(definition: KpiDefinition) {
    setEditingId(definition.id);
    setDraft(definitionDraft(definition));
    setShowEditor(true);
    setManualDefinition(null);
    setError("");
  }

  function closeEditor() {
    setShowEditor(false);
    setEditingId(null);
    setDraft(EMPTY_DRAFT);
  }

  function openManualEntry(definition: KpiDefinition) {
    setManualDefinition(definition);
    setManualValue("");
    setManualObservedAt(localDateTimeValue());
    setShowEditor(false);
    setError("");
  }

  function closeManualEntry() {
    setManualDefinition(null);
    setManualValue("");
  }

  function updateDraft(name: keyof KpiDraft, value: string) {
    setDraft((current) => ({ ...current, [name]: value }));
  }

  function inputPayload(): CreateKpiDefinitionInput | null {
    if (!selectedWorkspace) return null;
    const optionalDecimal = (value: string) => value.trim() || null;
    return {
      account_id: selectedWorkspace.account_id,
      space_id: selectedWorkspace.space_id,
      key: draft.key.trim(),
      name: draft.name.trim(),
      description: draft.description.trim(),
      category: draft.category.trim(),
      unit: draft.unit.trim(),
      source_label: draft.source_label.trim(),
      owner_label: draft.owner_label.trim(),
      freshness_minutes: Number(draft.freshness_minutes),
      warning_min: optionalDecimal(draft.warning_min),
      warning_max: optionalDecimal(draft.warning_max),
      critical_min: optionalDecimal(draft.critical_min),
      critical_max: optionalDecimal(draft.critical_max),
      display_order: Number(draft.display_order),
    };
  }

  async function saveDefinition(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const payload = inputPayload();
    if (!payload) return;
    setBusyAction("save");
    setError("");
    setNotice("");
    try {
      if (editingId) {
        await updateKpiDefinition(editingId, payload);
        setNotice("KPI definition updated.");
      } else {
        await createKpiDefinition(payload);
        setNotice("KPI definition created. It is ready to receive observations.");
      }
      closeEditor();
      setRefreshVersion((version) => version + 1);
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : "Could not save the KPI definition.");
    } finally {
      setBusyAction("");
    }
  }

  async function setDefinitionStatus(definition: KpiDefinition, status: "active" | "archived") {
    if (!selectedWorkspace) return;
    setBusyAction(`status:${definition.id}`);
    setError("");
    setNotice("");
    try {
      await updateKpiDefinition(definition.id, {
        account_id: selectedWorkspace.account_id,
        space_id: selectedWorkspace.space_id,
        status,
      });
      setNotice(status === "archived" ? `${definition.name} archived.` : `${definition.name} restored.`);
      closeEditor();
      setRefreshVersion((version) => version + 1);
    } catch (statusError) {
      setError(statusError instanceof Error ? statusError.message : "Could not update KPI status.");
    } finally {
      setBusyAction("");
    }
  }

  async function recordObservation(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedWorkspace || !manualDefinition) return;
    setBusyAction("manual");
    setError("");
    setNotice("");
    try {
      const observedAt = new Date(manualObservedAt);
      if (Number.isNaN(observedAt.getTime())) throw new Error("Choose a valid observation time.");
      await createManualKpiSnapshot(manualDefinition.id, {
        account_id: selectedWorkspace.account_id,
        space_id: selectedWorkspace.space_id,
        value: manualValue.trim(),
        observed_at: observedAt.toISOString(),
        source_ref: "manual-dashboard",
        idempotency_key: `manual:${manualDefinition.id}:${crypto.randomUUID()}`,
      });
      setNotice(`Observation recorded for ${manualDefinition.name}.`);
      closeManualEntry();
      setRefreshVersion((version) => version + 1);
    } catch (recordError) {
      setError(recordError instanceof Error ? recordError.message : "Could not record the observation.");
    } finally {
      setBusyAction("");
    }
  }

  const canConfigure = selectedWorkspace?.can_configure ?? dashboard?.can_configure ?? false;
  const canWriteManual = selectedWorkspace?.can_write_manual ?? dashboard?.can_write_manual ?? false;

  return (
    <div className="kpiWorkspace">
      <PageHeader
        description="Track the measures that matter and understand what changed."
        eyebrow="Operating ledger"
        title="KPIs"
        meta={selectedWorkspace ? (
          <>
            <StatusBadge tone="neutral">{selectedWorkspace.account_name}</StatusBadge>
            <StatusBadge tone="running">{selectedWorkspace.space_name}</StatusBadge>
            <StatusBadge tone="success">live observations</StatusBadge>
          </>
        ) : <StatusBadge tone="neutral">No workspace selected</StatusBadge>}
        actions={(
          <div className="kpiHeaderActions">
            <label className="compactField kpiWorkspacePicker">
              <span>Workspace</span>
              <select
                disabled={loadingWorkspaces || workspaces.length === 0}
                value={selectedWorkspaceKey}
                onChange={(event) => chooseWorkspace(event.target.value)}
              >
                {workspaces.length === 0 ? <option value="">No KPI workspace</option> : null}
                {workspaces.map((workspace) => (
                  <option key={workspaceKey(workspace)} value={workspaceKey(workspace)}>
                    {workspace.account_name} / {workspace.space_name}
                  </option>
                ))}
              </select>
            </label>
            <button
              className="secondaryButton"
              disabled={!selectedWorkspace || loadingDashboard}
              type="button"
              onClick={() => setRefreshVersion((version) => version + 1)}
            >
              {loadingDashboard ? "Refreshing" : "Refresh"}
            </button>
            {canConfigure ? <button className="primaryButton" type="button" onClick={openCreate}>Add KPI</button> : null}
          </div>
        )}
      />

      {error ? <Notice tone="error">{error}</Notice> : null}
      {notice ? <Notice tone="success">{notice}</Notice> : null}

      <MetricStrip
        metrics={[
          { label: "active", value: summary.active },
          { label: "healthy", tone: "success", value: summary.healthy },
          { label: "needs attention", tone: summary.attention ? "warning" : undefined, value: summary.attention },
          { label: "critical", tone: summary.critical ? "danger" : undefined, value: summary.critical },
          { label: "awaiting data", value: summary.awaiting },
        ]}
      />

      {canConfigure ? (
        <div className="kpiLedgerControls">
          <label>
            <input checked={includeArchived} type="checkbox" onChange={(event) => setIncludeArchived(event.target.checked)} />
            Include archived definitions
          </label>
          <span>30-observation horizon</span>
        </div>
      ) : null}

      <section className="kpiLedger" aria-label="KPI operating ledger" aria-busy={loadingDashboard}>
        <div className="kpiLedgerHead" aria-hidden="true">
          <span>Indicator</span>
          <span>Current</span>
          <span>Change</span>
          <span>State</span>
          <span>Observation horizon</span>
          <span>Actions</span>
        </div>

        {loadingWorkspaces || (loadingDashboard && !dashboard) ? (
          <div className="kpiLedgerLoading">Loading the operating ledger…</div>
        ) : null}

        {!loadingWorkspaces && !selectedWorkspace && !error ? (
          <div className="emptyState kpiEmptyState">
            <span className="emptyMark">KPI app</span>
            <h2>No readable KPI workspace</h2>
            <p>Install KPI Dashboard for an accessible space and allow the kpi_read purpose.</p>
          </div>
        ) : null}

        {dashboard && dashboard.items.length === 0 ? (
          <div className="emptyState kpiEmptyState">
            <span className="emptyMark">Ready</span>
            <h2>No KPI definitions yet</h2>
            <p>{canConfigure ? "Create the first indicator, then send observations through the service API or record a manual value." : "An administrator needs to configure the first indicator for this workspace."}</p>
            {canConfigure ? <button className="primaryButton" type="button" onClick={openCreate}>Create first KPI</button> : null}
          </div>
        ) : null}

        {dashboard?.items.map((item) => (
          <article className={`kpiLedgerRow ${item.definition.status}`} key={item.definition.id}>
            <div className="kpiIdentity">
              <span>{item.definition.category || "General"}</span>
              <strong>{item.definition.name}</strong>
              <small>{[item.definition.owner_label, item.definition.source_label].filter(Boolean).join(" · ") || item.definition.key}</small>
            </div>
            <div className="kpiCurrent">
              <strong>{formatValue(item.latest?.value ?? null, item.definition.unit)}</strong>
              <small>{formatObservedAt(item.latest?.observed_at)}</small>
            </div>
            <div className="kpiDelta">
              <strong>{formatDelta(item)}</strong>
              <small>from prior observation</small>
            </div>
            <div className="kpiState">
              <StatusBadge tone={thresholdTone(item.threshold_state)}>{item.threshold_state.replaceAll("_", " ")}</StatusBadge>
              {item.freshness_state === "stale" ? <StatusBadge tone="warning">stale</StatusBadge> : null}
              {item.definition.status === "archived" ? <StatusBadge tone="neutral">archived</StatusBadge> : null}
            </div>
            <ObservationHorizon item={item} />
            <div className="kpiRowActions">
              {canWriteManual && item.definition.status === "active" ? (
                <button className="textButton" type="button" onClick={() => openManualEntry(item.definition)}>Record</button>
              ) : null}
              {canConfigure ? <button className="textButton" type="button" onClick={() => openEdit(item.definition)}>Edit</button> : null}
            </div>
          </article>
        ))}
      </section>

      {showEditor && selectedWorkspace ? (
        <Panel
          eyebrow="Definition"
          title={editingId ? "Edit KPI" : "Create KPI"}
          actions={<button className="secondaryButton" type="button" onClick={closeEditor}>Close</button>}
        >
          <form className="kpiEditor" onSubmit={(event) => void saveDefinition(event)}>
            <div className="kpiEditorGrid">
              <DraftField required label="Name" maxLength={120} name="name" value={draft.name} onChange={updateDraft} />
              <DraftField required label="Stable key" maxLength={64} name="key" pattern="[a-z][a-z0-9_]+" placeholder="monthly_recurring_revenue" value={draft.key} onChange={updateDraft} />
              <DraftField label="Category" maxLength={80} name="category" placeholder="Revenue" value={draft.category} onChange={updateDraft} />
              <DraftField label="Unit" maxLength={32} name="unit" placeholder="EUR, %, accounts" value={draft.unit} onChange={updateDraft} />
              <DraftField label="Owner" maxLength={120} name="owner_label" placeholder="Finance" value={draft.owner_label} onChange={updateDraft} />
              <DraftField label="Source" maxLength={120} name="source_label" placeholder="Billing API" value={draft.source_label} onChange={updateDraft} />
              <DraftField required label="Fresh after (minutes)" min="1" max="525600" name="freshness_minutes" type="number" value={draft.freshness_minutes} onChange={updateDraft} />
              <DraftField required label="Display order" min="-1000000" max="1000000" name="display_order" type="number" value={draft.display_order} onChange={updateDraft} />
            </div>
            <label className="field">
              <span className="fieldLabel">Description</span>
              <textarea className="textarea" maxLength={500} value={draft.description} onChange={(event) => updateDraft("description", event.target.value)} />
            </label>
            <div className="kpiThresholds">
              <div>
                <strong>Lower bounds</strong>
                <small>critical ≤ warning</small>
                <DraftField label="Critical minimum" name="critical_min" type="number" step="any" value={draft.critical_min} onChange={updateDraft} />
                <DraftField label="Warning minimum" name="warning_min" type="number" step="any" value={draft.warning_min} onChange={updateDraft} />
              </div>
              <div>
                <strong>Upper bounds</strong>
                <small>warning ≤ critical</small>
                <DraftField label="Warning maximum" name="warning_max" type="number" step="any" value={draft.warning_max} onChange={updateDraft} />
                <DraftField label="Critical maximum" name="critical_max" type="number" step="any" value={draft.critical_max} onChange={updateDraft} />
              </div>
            </div>
            <div className="kpiEditorActions">
              {editingId ? (
                <button
                  className="textButton dangerLink"
                  disabled={busyAction === `status:${editingId}`}
                  type="button"
                  onClick={() => {
                    const definition = dashboard?.items.find((item) => item.definition.id === editingId)?.definition;
                    if (definition) void setDefinitionStatus(definition, definition.status === "archived" ? "active" : "archived");
                  }}
                >
                  {dashboard?.items.find((item) => item.definition.id === editingId)?.definition.status === "archived" ? "Restore KPI" : "Archive KPI"}
                </button>
              ) : <span />}
              <button className="primaryButton" disabled={busyAction === "save"} type="submit">
                {busyAction === "save" ? "Saving" : editingId ? "Save changes" : "Create KPI"}
              </button>
            </div>
          </form>
        </Panel>
      ) : null}

      {manualDefinition && selectedWorkspace ? (
        <Panel
          eyebrow="Manual observation"
          title={manualDefinition.name}
          actions={<button className="secondaryButton" type="button" onClick={closeManualEntry}>Close</button>}
        >
          <form className="kpiManualForm" onSubmit={(event) => void recordObservation(event)}>
            <label className="field">
              <span className="fieldLabel">Value {manualDefinition.unit ? `(${manualDefinition.unit})` : ""}</span>
              {/* eslint-disable-next-line jsx-a11y/no-autofocus -- this form only renders after the operator opens manual entry, so focus lands where they just asked to type; it is not a focus jump on page load. */}
              <input required autoFocus className="input" step="any" type="number" value={manualValue} onChange={(event) => setManualValue(event.target.value)} />
            </label>
            <label className="field">
              <span className="fieldLabel">Observed at</span>
              <input required className="input" type="datetime-local" value={manualObservedAt} onChange={(event) => setManualObservedAt(event.target.value)} />
            </label>
            <p>Manual values are immutable observations and are audit-logged without exposing their value.</p>
            <button className="primaryButton" disabled={busyAction === "manual"} type="submit">
              {busyAction === "manual" ? "Recording" : "Record observation"}
            </button>
          </form>
        </Panel>
      ) : null}
    </div>
  );
}
