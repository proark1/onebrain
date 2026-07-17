import type { ReactNode } from "react";
import { StatusBadge } from "@/components/admin-ui";
import { type OperationalStatus } from "@/lib/operational";
import { Timestamp } from "./timestamp";

type StatusSummaryProps = {
  children?: ReactNode;
  className?: string;
  status: OperationalStatus;
  updatedAt?: string | null;
  updatedLabel?: string;
};

/** Keeps condition, reason, action, and freshness together before diagnostics. */
export function StatusSummary({
  children,
  className = "",
  status,
  updatedAt,
  updatedLabel,
}: StatusSummaryProps) {
  return (
    <section className={`statusSummary ${status.tone} ${className}`.trim()}>
      <div className="statusSummaryLead">
        <StatusBadge tone={status.tone}>{status.condition}</StatusBadge>
        <p>{status.explanation}</p>
      </div>
      <div className="statusSummaryAction">
        <span>Next action</span>
        <strong>{status.nextAction}</strong>
        <Timestamp label={updatedLabel} value={updatedAt} />
      </div>
      {children ? <div className="statusSummaryExtra">{children}</div> : null}
    </section>
  );
}
