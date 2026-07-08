"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import type { FormEvent } from "react";

type LoginPanelProps = {
  nextPath: string;
};

export function LoginPanel({ nextPath }: LoginPanelProps) {
  const router = useRouter();
  const [error, setError] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);

  async function submitLogin(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setIsSubmitting(true);

    const form = new FormData(event.currentTarget);
    const email = String(form.get("email") || "").trim();
    const password = String(form.get("password") || "");

    try {
      const response = await fetch("/api/onebrain/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
      });

      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        setError(typeof body.detail === "string" ? body.detail : "Could not sign in.");
        return;
      }

      router.replace(nextPath);
      router.refresh();
    } catch {
      setError("Could not reach OneBrain. Please try again.");
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <main className="stateScreen">
      <section className="statePanel loginPanel">
        <div className="brand">
          <span className="brandMark">one</span>
          <span>brain</span>
        </div>
        <div className="loginHeading">
          <h1>Sign in</h1>
          <p>Use your OneBrain admin credentials to open the console.</p>
        </div>
        <form className="loginForm" onSubmit={submitLogin}>
          <label className="field">
            <span className="fieldLabel">Email</span>
            <input
              autoComplete="username"
              className="input"
              name="email"
              required
              type="email"
            />
          </label>
          <label className="field">
            <span className="fieldLabel">Password</span>
            <input
              autoComplete="current-password"
              className="input"
              name="password"
              required
              type="password"
            />
          </label>
          {error ? <p className="inlineError" role="alert">{error}</p> : null}
          <button className="primaryButton" disabled={isSubmitting} type="submit">
            {isSubmitting ? "Signing in..." : "Sign in"}
          </button>
        </form>
      </section>
    </main>
  );
}
