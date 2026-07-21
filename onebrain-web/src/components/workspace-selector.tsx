"use client";

import { useWorkspace } from "@/components/workspace-provider";

export function WorkspaceSelector() {
  const {
    accounts,
    available,
    error,
    loading,
    selectedAccountId,
    selectedSpaceId,
    selectedSpaceKind,
    setSelectedAccountId,
    setSelectedSpaceId,
    spaces,
  } = useWorkspace();

  // A failed scope load is not the same as having nothing to scope. Silently
  // returning null told the user their workspace controls did not exist, while
  // every request they made afterwards ran unscoped.
  if (error) {
    return (
      <section className="workspaceSelector" aria-label="Workspace scope">
        <div className="workspaceSelectorHead">
          <span>Workspace</span>
          <strong>Unavailable</strong>
        </div>
        <p className="inlineError" role="alert">{error}</p>
      </section>
    );
  }

  if (!available) {
    return null;
  }

  return (
    <section className="workspaceSelector" aria-label="Workspace scope">
      <div className="workspaceSelectorHead">
        <span>Workspace</span>
        <strong>{selectedSpaceKind}</strong>
      </div>

      <label className="compactField">
        <span>Account</span>
        <select
          disabled={loading || accounts.length === 0}
          value={selectedAccountId}
          onChange={(event) => setSelectedAccountId(event.target.value)}
        >
          {accounts.map((account) => (
            <option key={account.id} value={account.id}>
              {account.name || account.id}
            </option>
          ))}
        </select>
      </label>

      <label className="compactField">
        <span>Space</span>
        <select
          disabled={loading || !selectedAccountId}
          value={selectedSpaceId}
          onChange={(event) => setSelectedSpaceId(event.target.value)}
        >
          <option value="">All visible data</option>
          {spaces.map((space) => (
            <option key={space.id} value={space.id}>
              {space.name || space.id}
            </option>
          ))}
        </select>
      </label>
    </section>
  );
}
