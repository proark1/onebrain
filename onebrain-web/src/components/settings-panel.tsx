"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { PageHeader, Panel } from "@/components/admin-ui";

export function SettingsPanel() {
  const router = useRouter();
  const [error, setError] = useState("");
  const [isLoggingOut, setIsLoggingOut] = useState(false);

  async function logout() {
    setError("");
    setIsLoggingOut(true);
    try {
      const response = await fetch("/api/auth/logout", { method: "POST" });
      if (!response.ok) {
        setError("Could not log out. Please try again.");
        return;
      }
      router.replace("/login");
      router.refresh();
    } catch {
      setError("Could not reach OneBrain. Please try again.");
    } finally {
      setIsLoggingOut(false);
    }
  }

  return (
    <div className="adminSurface settingsSurface">
      <PageHeader description="Manage your security and the current OneBrain session." eyebrow="Account" title="Settings" />
      <Panel eyebrow="Security" title="Password">
        <div className="settingsActions">
          <Link className="settingsAction" href="/settings/password">
            <span><strong>Change password</strong><small>Update your password and sign in again.</small></span>
            <span aria-hidden="true">→</span>
          </Link>
        </div>
      </Panel>
      <Panel eyebrow="Current device" title="Session">
        <div className="settingsActions">
          <button className="settingsAction dangerAction" disabled={isLoggingOut} onClick={logout} type="button">
            <span><strong>{isLoggingOut ? "Logging out..." : "Log out"}</strong><small>End this signed-in session on this device.</small></span>
            <span aria-hidden="true">→</span>
          </button>
          {error ? <p className="inlineError" role="alert">{error}</p> : null}
        </div>
      </Panel>
    </div>
  );
}
