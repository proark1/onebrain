import { useId } from "react";
import type {
  DriveAudience,
  DriveFilingPolicy,
  DrivePolicyMode,
} from "./types";
import styles from "./drive.module.css";

export const DEFAULT_DRIVE_AUDIENCE: DriveAudience = {
  classifications: ["internal"],
  locations: ["global"],
  departments: [{ id: "general", name: "Everyone" }],
};

export function defaultDrivePolicy(
  audience: DriveAudience,
  canIndex: boolean,
): DriveFilingPolicy {
  return {
    classification: audience.classifications[0] ?? "internal",
    location: audience.locations[0] ?? "global",
    category: audience.departments[0]?.id ?? "general",
    indexForAi: canIndex,
  };
}

export function entryDrivePolicy(entry: {
  classification: string;
  location: string;
  category: string;
  desired_indexed?: boolean;
}): DriveFilingPolicy {
  return {
    classification: entry.classification,
    location: entry.location,
    category: entry.category,
    indexForAi: Boolean(entry.desired_indexed),
  };
}

export function drivePolicyWidens(
  previous: DriveFilingPolicy,
  next: DriveFilingPolicy,
  audience: DriveAudience,
): boolean {
  const previousRank = audience.classifications.indexOf(previous.classification);
  const nextRank = audience.classifications.indexOf(next.classification);
  return Boolean(
    (previousRank >= 0 && nextRank >= 0 && nextRank < previousRank)
    || (previous.location !== "global" && next.location !== previous.location)
    || (previous.category !== "general" && next.category !== previous.category)
    || (!previous.indexForAi && next.indexForAi)
  );
}

export function DriveFilingPolicyFields({
  audience,
  canIndex,
  policy,
  policyMode,
  onChange,
}: {
  audience: DriveAudience;
  canIndex: boolean;
  policy: DriveFilingPolicy;
  policyMode: DrivePolicyMode;
  onChange: (policy: DriveFilingPolicy) => void;
}) {
  const fieldId = useId();
  const classifications = includeCurrent(audience.classifications, policy.classification);
  const locations = includeCurrent(audience.locations, policy.location);
  const departments = audience.departments.some((item) => item.id === policy.category)
    ? audience.departments
    : [{ id: policy.category, name: humanize(policy.category) }, ...audience.departments];
  return (
    <div className={styles.policyFields}>
      <div className={styles.policyGrid}>
        <label htmlFor={`${fieldId}-classification`}>
          <span>Sensitivity</span>
          <select
            id={`${fieldId}-classification`}
            value={policy.classification}
            onChange={(event) => onChange({ ...policy, classification: event.target.value })}
          >
            {classifications.map((value) => <option key={value} value={value}>{humanize(value)}</option>)}
          </select>
        </label>
        <label htmlFor={`${fieldId}-department`}>
          <span>Department</span>
          <select
            id={`${fieldId}-department`}
            value={policy.category}
            onChange={(event) => onChange({ ...policy, category: event.target.value })}
          >
            {departments.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
          </select>
        </label>
        <label htmlFor={`${fieldId}-location`}>
          <span>Location</span>
          <select
            id={`${fieldId}-location`}
            value={policy.location}
            onChange={(event) => onChange({ ...policy, location: event.target.value })}
          >
            {locations.map((value) => <option key={value} value={value}>{humanize(value)}</option>)}
          </select>
        </label>
      </div>
      <label className={styles.aiToggle} htmlFor={`${fieldId}-index`}>
        <input
          checked={canIndex && policy.indexForAi}
          disabled={!canIndex}
          id={`${fieldId}-index`}
          type="checkbox"
          onChange={(event) => onChange({ ...policy, indexForAi: event.target.checked })}
        />
        <span>
          <strong>Index for AI</strong>
          <small>{indexingCopy(policyMode)}</small>
        </span>
      </label>
    </div>
  );
}

function includeCurrent(values: string[], current: string): string[] {
  return values.includes(current) ? values : [current, ...values].filter(Boolean);
}

function indexingCopy(mode: DrivePolicyMode): string {
  if (mode === "storage_only") return "Storage-only mode is active. AI cannot use files in this folder.";
  if (mode === "disabled") return "Drive changes are disabled for this deployment.";
  return "Files can be used in permitted AI answers after policy checks and approval.";
}

function humanize(value: string): string {
  return value.replace(/[_-]+/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}
