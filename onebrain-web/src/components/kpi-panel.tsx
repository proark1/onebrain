"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { MetricStrip, Notice, PageHeader, Panel, StatusBadge } from "@/components/admin-ui";
import { listOperatorCustomers } from "@/lib/onebrain-client";
import type { OperatorCustomer } from "@/lib/onebrain-types";

type KpiDataPoint = {
  id: string;
  label: string;
  value: string;
  delta: string;
  unit: string;
  history: number[];
  tone: "success" | "warning" | "danger" | "neutral";
};

type KpiSource = {
  id: string;
  name: string;
  system: string;
  category: string;
  status: "active" | "attention" | "planned";
  freshness: string;
  owner: string;
  retention: string;
  metrics: KpiDataPoint[];
};

const BASE_SOURCES: KpiSource[] = [
  {
    id: "finance-suite",
    name: "Financial suite",
    system: "ERP / accounting APIs",
    category: "Finance",
    status: "active",
    freshness: "Synced 12 min ago",
    owner: "Finance",
    retention: "36 monthly snapshots stored in OneBrain",
    metrics: [
      { id: "cash_runway", label: "Cash runway", value: "14.2 mo", delta: "+1.1 mo", unit: "months", history: [11.8, 12.1, 12.7, 13.1, 14.2], tone: "success" },
      { id: "gross_margin", label: "Gross margin", value: "68%", delta: "+3%", unit: "%", history: [62, 63, 65, 65, 68], tone: "success" },
      { id: "open_invoices", label: "Open invoices", value: "$182k", delta: "-8%", unit: "USD", history: [240, 225, 210, 198, 182], tone: "warning" },
    ],
  },
  {
    id: "revenue-crm",
    name: "Revenue pipeline",
    system: "CRM + billing APIs",
    category: "Revenue",
    status: "active",
    freshness: "Synced 18 min ago",
    owner: "Sales",
    retention: "Daily snapshots retained for trend analysis",
    metrics: [
      { id: "arr", label: "ARR", value: "$4.8M", delta: "+12%", unit: "USD", history: [3.9, 4.1, 4.3, 4.5, 4.8], tone: "success" },
      { id: "pipeline", label: "Pipeline", value: "$1.2M", delta: "+6%", unit: "USD", history: [0.9, 1.0, 1.0, 1.1, 1.2], tone: "success" },
      { id: "churn_risk", label: "Churn risk", value: "7 accts", delta: "+2", unit: "accounts", history: [4, 5, 5, 6, 7], tone: "warning" },
    ],
  },
  {
    id: "delivery-ops",
    name: "Delivery operations",
    system: "Project + support APIs",
    category: "Operations",
    status: "attention",
    freshness: "Synced 41 min ago",
    owner: "Operations",
    retention: "Hourly operational snapshots cached",
    metrics: [
      { id: "on_time_delivery", label: "On-time delivery", value: "91%", delta: "-2%", unit: "%", history: [95, 94, 93, 93, 91], tone: "warning" },
      { id: "sla_health", label: "SLA health", value: "97%", delta: "+1%", unit: "%", history: [94, 95, 96, 96, 97], tone: "success" },
      { id: "blocked_work", label: "Blocked work", value: "12", delta: "+4", unit: "items", history: [6, 7, 8, 8, 12], tone: "danger" },
    ],
  },
  {
    id: "people-systems",
    name: "People health",
    system: "HRIS + engagement APIs",
    category: "People",
    status: "planned",
    freshness: "Waiting for API key",
    owner: "People",
    retention: "Monthly HR snapshots ready once connected",
    metrics: [
      { id: "headcount", label: "Headcount", value: "128", delta: "+5", unit: "people", history: [118, 121, 123, 126, 128], tone: "success" },
      { id: "hiring_plan", label: "Hiring plan", value: "82%", delta: "on track", unit: "%", history: [72, 75, 78, 80, 82], tone: "success" },
      { id: "pulse_score", label: "Pulse score", value: "4.2", delta: "pending", unit: "score", history: [4.0, 4.0, 4.1, 4.1, 4.2], tone: "neutral" },
    ],
  },
];

function statusTone(status: KpiSource["status"]): "neutral" | "running" | "success" | "warning" {
  if (status === "active") return "success";
  if (status === "attention") return "warning";
  return "neutral";
}

export function KpiPanel() {
  const [customers, setCustomers] = useState<OperatorCustomer[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const loadSources = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setCustomers(await listOperatorCustomers());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load connected app context.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    queueMicrotask(() => {
      void loadSources();
    });
  }, [loadSources]);

  const connectedApps = useMemo(() => customers.flatMap((customer) => customer.apps), [customers]);
  const kpiInstallations = connectedApps.filter((app) => app.app_id === "kpi_dashboard").length;
  const allDataPoints = BASE_SOURCES.flatMap((source) => source.metrics);
  const activeSources = BASE_SOURCES.filter((source) => source.status === "active").length;
  const attentionSources = BASE_SOURCES.filter((source) => source.status === "attention").length;
  const plannedSources = BASE_SOURCES.filter((source) => source.status === "planned").length;

  return (
    <div className="kpiWorkspace">
      <PageHeader
        eyebrow="Structured intelligence"
        title="KPI dashboard"
        meta={(
          <>
            <StatusBadge tone="success">finance-ready</StatusBadge>
            <StatusBadge tone="running">API sourced</StatusBadge>
            <StatusBadge tone="neutral">feature app</StatusBadge>
          </>
        )}
        actions={(
          <button className="secondaryButton" disabled={loading} type="button" onClick={() => void loadSources()}>
            {loading ? "Refreshing" : "Refresh sources"}
          </button>
        )}
      />

      {error ? <Notice tone="warning">Showing KPI structure with sample values. Connected app context could not load: {error}</Notice> : null}

      <section className="kpiHero" aria-label="KPI dashboard purpose">
        <div>
          <p className="eyebrow">Why this exists</p>
          <h2>Executives can monitor the KPI Dashboard feature without asking the bot first.</h2>
          <p>
            Like Personal Assistant and AI Communication, KPI Dashboard is a first-class app on top of OneBrain.
            Companies can add many custom data points across finance, revenue, operations, people, or any other API. New snapshots are stored in OneBrain so historical dashboards do not need to repeatedly re-pull old records from each source platform.
          </p>
        </div>
        <div className="kpiHeroCard">
          <span>Connected app context</span>
          <strong>{kpiInstallations || connectedApps.length || "-"}</strong>
          <small>{kpiInstallations ? "KPI Dashboard app installations available" : connectedApps.length ? "apps available to map into KPI tiles" : "install KPI Dashboard in Apps to replace sample tiles"}</small>
        </div>
      </section>

      <MetricStrip
        metrics={[
          { label: "active sources", tone: "success", value: activeSources },
          { label: "needs attention", tone: attentionSources ? "warning" : "success", value: attentionSources },
          { label: "data points", value: allDataPoints.length },
          { label: "planned APIs", value: plannedSources },
          { label: "KPI app installs", value: kpiInstallations || "-" },
        ]}
      />

      <section className="kpiGrid" aria-label="KPI sources">
        {BASE_SOURCES.map((source) => (
          <Panel eyebrow={source.category} title={source.name} key={source.id} actions={<StatusBadge tone={statusTone(source.status)}>{source.status}</StatusBadge>}>
            <div className="kpiSourceMeta">
              <span>{source.system}</span>
              <span>{source.owner}</span>
              <span>{source.freshness}</span>
              <span>{source.retention}</span>
            </div>
            <div className="kpiMetricList">
              {source.metrics.map((metric) => (
                <article className={`kpiMetric ${metric.tone}`} key={metric.label}>
                  <span>{metric.label}</span>
                  <strong>{metric.value}</strong>
                  <small>{metric.delta} / {metric.unit}</small>
                  <div className="kpiSparkline" aria-label={`${metric.label} history`}>
                    {metric.history.map((point, index) => (
                      <span key={`${metric.id}_${index}`} style={{ height: `${Math.max(18, Math.min(100, point))}%` }} />
                    ))}
                  </div>
                </article>
              ))}
            </div>
          </Panel>
        ))}
      </section>

      <Panel eyebrow="OneBrain history" title="Saved KPI snapshots">
        <div className="kpiHistoryPlan">
          <div>
            <strong>Flexible schema</strong>
            <span>Every KPI is a named data point with source, owner, unit, freshness, and access policy.</span>
          </div>
          <div>
            <strong>Snapshot storage</strong>
            <span>Connector jobs should save periodic KPI values in OneBrain so charts can read local history.</span>
          </div>
          <div>
            <strong>Source refresh</strong>
            <span>External APIs are used for new deltas; old values stay queryable from OneBrain.</span>
          </div>
        </div>
      </Panel>
    </div>
  );
}
