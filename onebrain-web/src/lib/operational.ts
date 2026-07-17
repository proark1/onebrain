export type OperationalTone = "danger" | "neutral" | "running" | "success" | "warning";

export type OperationalStatus = {
  condition: string;
  explanation: string;
  nextAction: string;
  tone: OperationalTone;
};

export type OperationalTimestamp = {
  dateTime: string;
  isMissing: boolean;
  local: string;
  relative: string;
};

type TimestampOptions = {
  locale?: string;
  now?: Date;
  timeZone?: string;
};

const DATE_TIME_FORMAT: Intl.DateTimeFormatOptions = {
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  month: "short",
  year: "numeric",
};

function missingTimestamp(): OperationalTimestamp {
  return {
    dateTime: "",
    isMissing: true,
    local: "No signal received yet",
    relative: "Not yet reported",
  };
}

function relativeAge(timestamp: Date, now: Date): string {
  const differenceSeconds = Math.round((timestamp.getTime() - now.getTime()) / 1000);
  const absoluteSeconds = Math.abs(differenceSeconds);
  const formatter = new Intl.RelativeTimeFormat("en", { numeric: "auto" });

  if (absoluteSeconds < 45) return formatter.format(0, "second");
  if (absoluteSeconds < 45 * 60) return formatter.format(Math.round(differenceSeconds / 60), "minute");
  if (absoluteSeconds < 22 * 60 * 60) return formatter.format(Math.round(differenceSeconds / 3600), "hour");
  if (absoluteSeconds < 6 * 24 * 60 * 60) return formatter.format(Math.round(differenceSeconds / 86400), "day");
  return formatter.format(Math.round(differenceSeconds / 604800), "week");
}

/** Formats an API time for an operator without concealing an absent signal. */
export function formatOperationalTimestamp(value: string | null | undefined, options: TimestampOptions = {}): OperationalTimestamp {
  if (!value) return missingTimestamp();

  const timestamp = new Date(value);
  if (Number.isNaN(timestamp.getTime())) return missingTimestamp();

  return {
    dateTime: timestamp.toISOString(),
    isMissing: false,
    local: new Intl.DateTimeFormat(options.locale, {
      ...DATE_TIME_FORMAT,
      timeZone: options.timeZone,
    }).format(timestamp),
    relative: relativeAge(timestamp, options.now ?? new Date()),
  };
}

/** Converts raw service states into a concise condition, explanation, and next action. */
export function describeOperationalStatus(value: string | null | undefined): OperationalStatus {
  const normalized = (value ?? "").trim().toLowerCase().replaceAll("_", "-");

  if (!normalized || ["none", "unknown", "not-reported", "no-data"].includes(normalized)) {
    return {
      condition: "Not yet reported",
      explanation: "No operational signal has been received yet.",
      nextAction: "Check the connection and wait for the first report.",
      tone: "neutral",
    };
  }

  if (["not-deployed", "not-ready"].includes(normalized)) {
    return {
      condition: "Pending",
      explanation: "This customer has not been deployed yet.",
      nextAction: "Choose an initial version in Control and start the first deployment.",
      tone: "warning",
    };
  }

  if (["healthy", "active", "clear", "success", "succeeded", "verified", "complete", "completed", "not-required"].includes(normalized)) {
    return {
      condition: "Healthy",
      explanation: "The latest report shows this service is operating normally.",
      nextAction: "No action needed. Continue monitoring.",
      tone: "success",
    };
  }

  if (["updating", "running", "deploying", "dispatched", "in-progress"].includes(normalized)) {
    return {
      condition: "Updating",
      explanation: "A change is in progress and the latest report has not completed yet.",
      nextAction: "Wait for the next report before taking further action.",
      tone: "running",
    };
  }

  if (["paused", "cancelled"].includes(normalized)) {
    return {
      condition: "Needs attention",
      explanation: normalized === "paused"
        ? "This work is paused and will not continue until an operator decides what to do."
        : "This work was cancelled before it completed.",
      nextAction: "Open the details and decide whether to resume, retry, or leave it stopped.",
      tone: "warning",
    };
  }

  if (["pending", "queued", "waiting", "scheduled", "retrying"].includes(normalized)) {
    return {
      condition: "Pending",
      explanation: "This work is waiting for a report or the next execution step.",
      nextAction: "Check that the responsible service is connected and reporting.",
      tone: "warning",
    };
  }

  if ([
    "failed",
    "backup-failed",
    "health-failed",
    "rollout-failed",
    "dispatch-failed",
    "critical",
    "error",
    "unhealthy",
    "blocked",
    "attention",
  ].includes(normalized)) {
    return {
      condition: "Needs attention",
      explanation: "The latest report indicates a failure that needs review.",
      nextAction: "Open the details, review the failure, and decide the recovery action.",
      tone: "danger",
    };
  }

  return {
    condition: "Needs attention",
    explanation: "The latest report needs an operator review before it can be trusted.",
    nextAction: "Open the details and confirm the next safe action.",
    tone: "warning",
  };
}
