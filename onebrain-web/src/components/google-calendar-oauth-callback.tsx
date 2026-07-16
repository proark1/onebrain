"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { completeGoogleCalendarOAuth } from "@/lib/onebrain-client";

function scopeFromState(state: string): { account_id: string; space_id: string } | null {
  try {
    const encoded = state.split(".", 1)[0];
    const normalized = encoded.replaceAll("-", "+").replaceAll("_", "/");
    const payload = JSON.parse(atob(normalized.padEnd(Math.ceil(normalized.length / 4) * 4, "="))) as Record<string, unknown>;
    const accountId = typeof payload.account_id === "string" ? payload.account_id : "";
    const spaceId = typeof payload.space_id === "string" ? payload.space_id : "";
    return accountId && spaceId ? { account_id: accountId, space_id: spaceId } : null;
  } catch {
    return null;
  }
}

export function GoogleCalendarOAuthCallback({ code, oauthError, state }: {
  code: string;
  oauthError: string;
  state: string;
}) {
  const started = useRef(false);
  const responseIncomplete = Boolean(oauthError || !code || !state);
  const [status, setStatus] = useState<"working" | "complete" | "failed">(
    responseIncomplete ? "failed" : "working",
  );
  const [message, setMessage] = useState(
    responseIncomplete
      ? oauthError ? `Google returned: ${oauthError}` : "The OAuth response is incomplete."
      : "Verifying the signed request and storing the credential securely…",
  );

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    if (responseIncomplete) return;
    async function finishConnection() {
      let savedScope: { account_id?: string; space_id?: string } = {};
      try {
        savedScope = JSON.parse(sessionStorage.getItem("onebrain.google-calendar.oauth") || "{}") as typeof savedScope;
      } catch {
        savedScope = {};
      }
      const signedScope = scopeFromState(state);
      const accountId = savedScope.account_id || signedScope?.account_id || "";
      const spaceId = savedScope.space_id || signedScope?.space_id || "";
      if (!accountId || !spaceId) {
        setStatus("failed");
        setMessage("The initiating OneBrain workspace could not be recovered. Start the connection again.");
        return;
      }
      try {
        await completeGoogleCalendarOAuth({ account_id: accountId, space_id: spaceId, state, code });
        sessionStorage.removeItem("onebrain.google-calendar.oauth");
        setStatus("complete");
        setMessage("Google Calendar is connected. Review employee and calendar grants before using it.");
      } catch (reason) {
        setStatus("failed");
        setMessage(reason instanceof Error ? reason.message : "Google Calendar could not be connected.");
      }
    }
    void finishConnection();
  }, [code, responseIncomplete, state]);

  return (
    <div className={`aiOAuthResult ${status}`}>
      <span>{status === "working" ? "…" : status === "complete" ? "✓" : "×"}</span>
      <p className="eyebrow">Google Workspace Calendar</p>
      <h1>{status === "working" ? "Finishing connection" : status === "complete" ? "Calendar connected" : "Connection stopped"}</h1>
      <p>{message}</p>
      {status !== "working" ? <Link href="/ai-employees">Return to AI Employees</Link> : null}
    </div>
  );
}
