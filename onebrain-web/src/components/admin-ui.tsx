import type { ReactNode } from "react";

type PageHeaderProps = {
  actions?: ReactNode;
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
  title: string;
};

export function PageHeader({ actions, eyebrow, meta, title }: PageHeaderProps) {
  return (
    <header className="pageHeader">
      <div className="pageHeaderMain">
        <p className="eyebrow">{eyebrow}</p>
        <h1>{title}</h1>
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

export function Tabs<T extends string>({ active, items, label, onChange }: TabsProps<T>) {
  return (
    <div className="tabBar" role="tablist" aria-label={label}>
      {items.map((item) => (
        <button
          aria-selected={active === item.id}
          className={active === item.id ? "active" : ""}
          key={item.id}
          role="tab"
          type="button"
          onClick={() => onChange(item.id)}
        >
          <span>{item.label}</span>
          {item.meta ? <small>{item.meta}</small> : null}
        </button>
      ))}
    </div>
  );
}

export function Panel({ actions, children, count, eyebrow, title }: PanelProps) {
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
