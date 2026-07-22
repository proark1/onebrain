import type { ReactNode } from "react";

type PageHeaderProps = {
  actions?: ReactNode;
  description?: ReactNode;
  eyebrow: string;
  meta?: ReactNode;
  title: string;
};

type Metric = {
  label: string;
  tone?: "danger" | "success" | "warning";
  value: ReactNode;
};

type TabItem<T extends string> = {
  id: T;
  label: string;
  meta?: ReactNode;
};

type TabsProps<T extends string> = {
  active: T;
  items: Array<TabItem<T>>;
  label: string;
  onChange: (id: T) => void;
};

type PanelProps = {
  actions?: ReactNode;
  children: ReactNode;
  count?: ReactNode;
  eyebrow?: string;
  /**
   * One or two plain sentences: what this panel is, and what the operator is
   * expected to do with it. These screens show raw control-plane state, and a
   * title like "Promotion ledger" does not tell a reader whether approving a
   * release ships anything. Say the consequence, not the noun.
   */
  intro?: ReactNode;
  title: string;
};

export function PageHeader({ actions, description, eyebrow, meta, title }: PageHeaderProps) {
  return (
    <header className="pageHeader">
      <div className="pageHeaderMain">
        <p className="eyebrow">{eyebrow}</p>
        <h1>{title}</h1>
        {description ? <div className="pageHeaderDescription">{description}</div> : null}
        {meta ? <div className="pageHeaderMeta">{meta}</div> : null}
      </div>
      {actions ? <div className="pageHeaderActions">{actions}</div> : null}
    </header>
  );
}

export function MetricStrip({ metrics }: { metrics: Metric[] }) {
  return (
    <section className="metricStrip" aria-label="Summary">
      {metrics.map((metric) => (
        <div className={metric.tone ? `metricCard ${metric.tone}` : "metricCard"} key={metric.label}>
          <strong>{metric.value}</strong>
          <span>{metric.label}</span>
        </div>
      ))}
    </section>
  );
}

// Deliberately NOT an ARIA tabs widget. The WAI-ARIA tabs pattern is a package
// deal: every `role="tab"` must point at a real `role="tabpanel"` via
// `aria-controls`, and the tablist must implement roving `tabIndex` plus
// Home/End/Arrow key navigation. This component renders only the button strip —
// each caller (fleet-panel, operator-panel, spaces-panel) owns its panel content
// and renders it as a sibling, so `Tabs` has no panel element to identify or
// label without an API change at all three call sites. The previous markup
// declared `role="tablist"`/`role="tab"`/`aria-selected` with none of that
// wiring, so a screen reader announced "tab 2 of 5", promised arrow keys that
// did nothing, and never associated a panel. Honest buttons in a labelled
// navigation landmark, with `aria-current` marking the active section, describe
// what this control actually is. `aria-current="true"` (not "page") because the
// buttons swap an in-page section rather than navigate between pages.
export function Tabs<T extends string>({ active, items, label, onChange }: TabsProps<T>) {
  return (
    <nav className="tabBar" aria-label={label}>
      {items.map((item) => (
        <button
          aria-current={active === item.id ? "true" : undefined}
          className={active === item.id ? "active" : ""}
          key={item.id}
          type="button"
          onClick={() => onChange(item.id)}
        >
          <span>{item.label}</span>
          {item.meta ? <small>{item.meta}</small> : null}
        </button>
      ))}
    </nav>
  );
}

export function Panel({ actions, children, count, eyebrow, intro, title }: PanelProps) {
  return (
    <section className="adminPanel">
      <div className="panelHead">
        <div>
          {eyebrow ? <p className="eyebrow">{eyebrow}</p> : null}
          <h2>{title}</h2>
        </div>
        <div className="panelActions">
          {count !== undefined ? <span className="countBadge">{count}</span> : null}
          {actions}
        </div>
      </div>
      {intro ? <p className="panelIntro">{intro}</p> : null}
      {children}
    </section>
  );
}

export function StatusBadge({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: "danger" | "neutral" | "running" | "success" | "warning";
}) {
  return <span className={`statusBadge ${tone}`}>{children}</span>;
}

export function Notice({ children, tone }: { children: ReactNode; tone: "error" | "success" | "warning" }) {
  return (
    <div className={`notice ${tone}`} role={tone === "error" ? "alert" : "status"}>
      {children}
    </div>
  );
}
