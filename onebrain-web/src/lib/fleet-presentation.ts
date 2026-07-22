import type { FleetOverview } from "@/lib/onebrain-types";
import type { OperationalStatus } from "@/lib/operational";

/**
 * Make a stored host safe to use as an href.
 *
 * `ProvisioningRun.external_run_url` is a bare hostname with no scheme, and it
 * degrades to a raw IP when the box was created with DNS disabled. A schemeless
 * value in an href is read as a *relative path*, so it silently navigates
 * within Mission Control instead of out to the box. Returns "" when there is
 * nothing linkable, so callers can render plain text instead of a dead link.
 *
 * The fleet overview's `login_url` already arrives absolute -- the control
 * plane applies the same rule server-side. This is for the raw provisioning
 * fields the operator ledger still renders itself.
 */
export function absoluteHostUrl(host: string): string {
  const value = (host || "").trim().replace(/\/+$/, "");
  if (!value) return "";
  if (value.startsWith("https://") || value.startsWith("http://")) return value;
  // An IPv4 literal means no DNS and therefore no certificate: the box serves
  // plain HTTP on :80, so https would only fail to connect.
  return /^\d{1,3}(\.\d{1,3}){3}$/.test(value) ? `http://${value}` : `https://${value}`;
}

export function fleetHealthTone(healthy: boolean | null): "success" | "danger" | "neutral" {
  if (healthy === null) return "neutral";
  return healthy ? "success" : "danger";
}

export function fleetHealthLabel(healthy: boolean | null): string {
  if (healthy === null) return "No signal";
  return healthy ? "Healthy" : "Needs attention";
}

export function describeFleetOverview(overview: FleetOverview): OperationalStatus {
  if (overview.with_open_alerts > 0) {
    return {
      condition: `${overview.with_open_alerts} deployment${overview.with_open_alerts === 1 ? " needs" : "s need"} attention`,
      explanation: "One or more deployments reported an open operational alert.",
      nextAction: "Expand the affected deployment and review its latest activity.",
      tone: "danger",
    };
  }
  if (overview.total > 0 && overview.healthy === overview.total) {
    return {
      condition: "All deployments are healthy",
      explanation: "Every deployment is reporting normally with no open alerts.",
      nextAction: "No action is needed. Continue monitoring release activity.",
      tone: "success",
    };
  }
  if (overview.total === 0) {
    return {
      condition: "No deployments enrolled",
      explanation: "Fleet has no deployments to monitor yet.",
      nextAction: "Open Enrollment keys to connect the first deployment.",
      tone: "neutral",
    };
  }
  const reviewCount = Math.max(overview.total - overview.healthy, 1);
  return {
    condition: `${reviewCount} deployment signal${reviewCount === 1 ? " needs" : "s need"} review`,
    explanation: "A deployment is unhealthy or has not reported enough information yet.",
    nextAction: "Expand the deployment and verify its latest report and release state.",
    tone: "warning",
  };
}
