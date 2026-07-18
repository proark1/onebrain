import type { DriveEntry } from "./types";

export type DriveStatusTone = "neutral" | "running" | "success" | "warning" | "danger";

export type DriveStatusPresentation = {
  label: string;
  detail: string;
  tone: DriveStatusTone;
};

const STATUS_PRESENTATION: Record<string, DriveStatusPresentation> = {
  not_indexed: { label: "Not indexed", detail: "AI does not use this file.", tone: "neutral" },
  stored: { label: "Not indexed", detail: "The original is stored; AI does not use it.", tone: "neutral" },
  queued: { label: "Queued", detail: "Waiting for AI processing.", tone: "running" },
  extracting: { label: "Preparing", detail: "Reading the original file.", tone: "running" },
  indexing: { label: "Indexing", detail: "Preparing this file for AI answers.", tone: "running" },
  indexed: { label: "Indexed", detail: "AI can use this file for permitted audiences.", tone: "success" },
  awaiting_review: { label: "Needs review", detail: "AI waits for an authorized review.", tone: "warning" },
  pending: { label: "Needs review", detail: "AI waits for an authorized review.", tone: "warning" },
  blocked: { label: "Blocked", detail: "Policy prevents AI from using this file.", tone: "danger" },
  quarantined: { label: "Blocked", detail: "Policy prevents AI from using this file.", tone: "danger" },
  unsupported: { label: "Unsupported", detail: "The original is safe, but AI cannot read this format.", tone: "warning" },
  failed: { label: "Failed", detail: "AI processing failed. The original remains stored.", tone: "danger" },
  stale: { label: "Updating", detail: "A newer file policy is being applied.", tone: "running" },
};

export function driveStatusPresentation(status: string): DriveStatusPresentation {
  return STATUS_PRESENTATION[status.toLowerCase()] ?? {
    label: status ? humanize(status) : "Not indexed",
    detail: "AI availability has not been reported yet.",
    tone: "neutral",
  };
}

export function formatDriveSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const unit = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  const value = bytes / (1024 ** unit);
  return `${value < 10 && unit > 0 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}

export function formatDriveDate(value: string): string {
  const date = new Date(value);
  if (!value || Number.isNaN(date.getTime())) return "Not yet reported";
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium" }).format(date);
}

export function driveAudienceSummary(entry: DriveEntry): string {
  return [entry.classification, entry.category, entry.location]
    .filter(Boolean)
    .map(humanize)
    .join(" · ") || "Company policy";
}

export function canDownloadDriveEntry(entry: DriveEntry): boolean {
  return entry.kind === "file" && !entry.legacy;
}

export function driveFileKind(name: string, mediaType: string): string {
  const extension = name.includes(".") ? name.split(".").pop()?.toUpperCase() : "";
  if (extension && extension.length <= 5) return extension;
  if (mediaType.startsWith("image/")) return "IMAGE";
  if (mediaType === "application/pdf") return "PDF";
  return "FILE";
}

function humanize(value: string): string {
  return value
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}
