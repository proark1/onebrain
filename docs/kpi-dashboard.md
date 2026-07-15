# KPI Dashboard

KPI Dashboard stores governed KPI definitions and immutable observation history
inside the customer's OneBrain deployment. The web dashboard reads this local
history; it does not call an ERP, CRM, billing system, or arbitrary remote URL.
Connector jobs submit new aggregate observations through the service API.

Do not put names, email addresses, subject identifiers, bearer tokens, or other
personal or secret data in KPI keys or source references. KPI observations are
customer content and participate in privacy export, erasure, legal holds, and
retention.

## Deployment prerequisites

1. Apply Alembic migration `0023_kpi_dashboard_data` with the migration owner:

   ```powershell
   python -m alembic upgrade head
   ```

2. Install the `kpi_dashboard` app for the target account and space with the
   purposes required by the deployment:

   - `kpi_read` allows authorized human members to read the dashboard;
   - `kpi_configure` allows account administrators to manage definitions;
   - `kpi_snapshot_write` allows account administrators to record a manual
     observation and service keys to ingest connector observations.

3. Give a connector only a KPI Dashboard key pinned to the account, the target
   space, write scope, and the `kpi_snapshot_write` purpose. Provisioning can
   mint this least-privilege key. Never expose it to the browser.

The KPI page lists only workspaces that the signed-in human can access and for
which `kpi_read` is installed. Configuration and manual entry controls appear
only for account administrators with the matching installation purpose.

## Connector ingestion

Send observations directly to the customer API with the scoped service key:

```http
POST /api/service/kpis/snapshots
Authorization: Bearer <service-key>
Content-Type: application/json

{
  "space_id": "sp_acme_finance",
  "snapshots": [
    {
      "kpi_key": "monthly_recurring_revenue",
      "value": "482000.00",
      "observed_at": "2026-07-16T10:00:00Z",
      "source_ref": "billing-daily-rollup",
      "idempotency_key": "billing:mrr:2026-07-16T10:00:00Z"
    }
  ]
}
```

Each item must provide exactly one of `kpi_key` or `kpi_id`. A request contains
1–100 items. Values are finite decimals with at most 10 decimal places, and an
observation cannot be more than five minutes in the future. The server derives
the account, app, and actor from the key and repeats authorization for every KPI.

The batch is transactional. An exact idempotent retry returns the existing
observation and increments `duplicate_count`; reuse of an idempotency key or an
observation timestamp with different data returns `409 Conflict`. Use a stable,
source-owned idempotency key so network retries cannot create duplicate history.

## History and lifecycle

The dashboard loads one bounded summary for the workspace, including the latest
and prior values plus a 30-observation horizon for each definition. Definitions
can be archived and restored; archiving preserves their history and excludes
them from the default ledger.

Privacy export includes KPI definitions and observations. Account or space
erasure deletes both after the existing legal-hold gate. KPI retention evaluates
only immutable, server-controlled `received_at` timestamps and deletes expired
observations while preserving definitions.

Postgres deployments enforce account and optional space scope with forced row
level security on both KPI tables. The in-memory backend persists KPI data in a
separate `kpis.json` file and is intended only for local or synthetic use.
