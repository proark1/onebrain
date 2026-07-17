import { formatOperationalTimestamp } from "@/lib/operational";

type TimestampProps = {
  className?: string;
  label?: string;
  value?: string | null;
};

/** A local time, relative age, and exact machine-readable timestamp. */
export function Timestamp({ className = "", label = "Last updated", value }: TimestampProps) {
  const timestamp = formatOperationalTimestamp(value);

  if (timestamp.isMissing) {
    return (
      <span className={`operationalTimestamp missing ${className}`.trim()}>
        <span>{label}: {timestamp.local}</span>
        <small>{timestamp.relative}</small>
      </span>
    );
  }

  return (
    <span className={`operationalTimestamp ${className}`.trim()}>
      <span>{label}: <time dateTime={timestamp.dateTime}>{timestamp.local}</time></span>
      <small>{timestamp.relative}</small>
    </span>
  );
}
