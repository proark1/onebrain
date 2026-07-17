"use client";

import { useId, useState, type ReactNode } from "react";

type ExpandableCardProps = {
  children: ReactNode;
  className?: string;
  defaultExpanded?: boolean;
  summary: ReactNode;
  title: ReactNode;
};

/** A compact card that keeps dense operational details opt-in and keyboard accessible. */
export function ExpandableCard({
  children,
  className = "",
  defaultExpanded = false,
  summary,
  title,
}: ExpandableCardProps) {
  const [expanded, setExpanded] = useState(defaultExpanded);
  const detailsId = useId();

  return (
    <article className={`expandableCard ${expanded ? "expanded" : ""} ${className}`.trim()}>
      <div className="expandableCardTop">
        <div className="expandableCardIdentity">{title}</div>
        <button
          aria-controls={detailsId}
          aria-expanded={expanded}
          className="expandableCardToggle"
          onClick={() => setExpanded((current) => !current)}
          type="button"
        >
          {expanded ? "Hide details" : "Expand"}
        </button>
      </div>
      <div className="expandableCardSummary">{summary}</div>
      <div className="expandableCardDetails" hidden={!expanded} id={detailsId}>{children}</div>
    </article>
  );
}
