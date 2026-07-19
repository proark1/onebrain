"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import type { FormEvent } from "react";
import { PageHeader, Panel } from "@/components/admin-ui";

export function PasswordChangePanel({ standalone = false }: { standalone?: boolean }) {
  const router = useRouter();
  const [error, setError] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    const form = new FormData(event.currentTarget);
    const currentPassword = String(form.get("current_password") || "");
    const newPassword = String(form.get("new_password") || "");
    const confirmation = String(form.get("confirmation") || "");

    if (newPassword.length < 12) {
      setError("Your new password must contain at least 12 characters.");
      return;
    }
    if (newPassword !== confirmation) {
      setError("The new password and confirmation do not match.");
      return;
    }

    setIsSubmitting(true);
    try {
      const response = await fetch("/api/auth/change-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        setError(typeof body.detail === "string" ? body.detail : "Could not change your password.");
        return;
      }
      await fetch("/api/auth/logout", { method: "POST" });
      router.replace("/login?passwordChanged=1");
      router.refresh();
    } catch {
      setError("Could not reach OneBrain. Please try again.");
    } finally {
      setIsSubmitting(false);
    }
  }

  const form = (
    <form className="loginForm accountForm" onSubmit={submit}>
      <label className="field"><span className="fieldLabel">Current password</span><input autoComplete="current-password" className="input" name="current_password" required type="password" /></label>
      <label className="field"><span className="fieldLabel">New password</span><input autoComplete="new-password" className="input" minLength={12} name="new_password" required type="password" /></label>
      <label className="field"><span className="fieldLabel">Confirm new password</span><input autoComplete="new-password" className="input" minLength={12} name="confirmation" required type="password" /></label>
      {error ? <p className="inlineError" role="alert">{error}</p> : null}
      <button className="primaryButton" disabled={isSubmitting} type="submit">{isSubmitting ? "Changing password…" : "Change password"}</button>
    </form>
  );

  if (standalone) {
    return (
      <main className="stateScreen">
        <section className="statePanel loginPanel">
          <div className="brand"><span className="brandMark">AD</span><span>OneBrain</span></div>
          <div className="loginHeading">
            <p className="eyebrow">Account security</p>
            <h1>Change password</h1>
            <p>Choose a new password with at least 12 characters. You will sign in again afterwards.</p>
          </div>
          {form}
        </section>
      </main>
    );
  }

  return (
    <div className="adminSurface settingsSurface">
      <PageHeader description="Choose a new password. You will sign in again after it changes." eyebrow="Account" title="Change password" />
      <Panel eyebrow="Security" title="Password">{form}</Panel>
    </div>
  );
}
