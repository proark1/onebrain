"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { listPlatformAccounts, listPlatformSpaces } from "@/lib/onebrain-client";
import type { ChatScope, PlatformAccount, PlatformSpace, SessionInfo } from "@/lib/onebrain-types";
import { cleanScope } from "@/lib/onebrain-types";

type WorkspaceContextValue = {
  accounts: PlatformAccount[];
  available: boolean;
  loading: boolean;
  scope: ChatScope;
  selectedAccountId: string;
  selectedSpaceId: string;
  selectedSpaceKind: string;
  setSelectedAccountId: (accountId: string) => void;
  setSelectedSpaceId: (spaceId: string) => void;
  spaces: PlatformSpace[];
};

const WorkspaceContext = createContext<WorkspaceContextValue | null>(null);

function emptyWorkspaceValue(): WorkspaceContextValue {
  return {
    accounts: [],
    available: false,
    loading: false,
    scope: {},
    selectedAccountId: "",
    selectedSpaceId: "",
    selectedSpaceKind: "All",
    setSelectedAccountId: () => {},
    setSelectedSpaceId: () => {},
    spaces: [],
  };
}

function spaceKindLabel(kind: string): string {
  const labels: Record<string, string> = {
    business: "Business",
    customer_service: "Customer service",
    family: "Family",
    personal: "Personal",
    project: "Project",
    shared: "Shared",
  };
  return labels[kind] || kind || "Space";
}

export function WorkspaceProvider({ children, session }: { children: ReactNode; session: SessionInfo }) {
  const isAdmin = session.role_id === "admin";
  const [accounts, setAccounts] = useState<PlatformAccount[]>([]);
  const [spaces, setSpaces] = useState<PlatformSpace[]>([]);
  const [selectedAccountId, setSelectedAccountIdState] = useState("");
  const [selectedSpaceId, setSelectedSpaceId] = useState("");
  const [available, setAvailable] = useState(false);
  const [loading, setLoading] = useState(isAdmin);

  useEffect(() => {
    let active = true;

    async function loadAccounts() {
      if (!isAdmin) {
        setLoading(false);
        return;
      }

      setLoading(true);
      try {
        const nextAccounts = await listPlatformAccounts();
        if (!active) {
          return;
        }
        const tenantAccount = nextAccounts.find((account) => account.id === session.tenant_id);
        setAccounts(tenantAccount ? [tenantAccount] : []);
        setSelectedAccountIdState(tenantAccount?.id ?? "");
        setAvailable(Boolean(tenantAccount));
      } catch {
        if (!active) {
          return;
        }
        setAccounts([]);
        setSpaces([]);
        setSelectedAccountIdState("");
        setSelectedSpaceId("");
        setAvailable(false);
      } finally {
        if (active) {
          setLoading(false);
        }
      }
    }

    void loadAccounts();
    return () => {
      active = false;
    };
  }, [isAdmin, session.tenant_id]);

  useEffect(() => {
    let active = true;

    async function loadSpaces() {
      if (!isAdmin || !selectedAccountId || !available) {
        setSpaces([]);
        setSelectedSpaceId("");
        return;
      }

      setLoading(true);
      try {
        const nextSpaces = await listPlatformSpaces(selectedAccountId);
        if (!active) {
          return;
        }
        setSpaces(nextSpaces);
        setSelectedSpaceId((current) => {
          if (current && nextSpaces.some((space) => space.id === current)) {
            return current;
          }
          return nextSpaces[0]?.id ?? "";
        });
      } catch {
        if (!active) {
          return;
        }
        setSpaces([]);
        setSelectedSpaceId("");
      } finally {
        if (active) {
          setLoading(false);
        }
      }
    }

    void loadSpaces();
    return () => {
      active = false;
    };
  }, [available, isAdmin, selectedAccountId]);

  const setSelectedAccountId = useCallback((accountId: string) => {
    setSelectedAccountIdState(accountId);
    setSelectedSpaceId("");
  }, []);

  const selectedSpace = useMemo(
    () => spaces.find((space) => space.id === selectedSpaceId) ?? null,
    [selectedSpaceId, spaces],
  );

  const value = useMemo<WorkspaceContextValue>(() => {
    if (!isAdmin) {
      return emptyWorkspaceValue();
    }
    return {
      accounts,
      available,
      loading,
      scope: cleanScope({ account_id: selectedAccountId, space_id: selectedSpaceId }),
      selectedAccountId,
      selectedSpaceId,
      selectedSpaceKind: selectedSpace ? spaceKindLabel(selectedSpace.kind) : "All",
      setSelectedAccountId,
      setSelectedSpaceId,
      spaces,
    };
  }, [
    accounts,
    available,
    isAdmin,
    loading,
    selectedAccountId,
    selectedSpace,
    selectedSpaceId,
    setSelectedAccountId,
    spaces,
  ]);

  return <WorkspaceContext.Provider value={value}>{children}</WorkspaceContext.Provider>;
}

export function useWorkspace() {
  const value = useContext(WorkspaceContext);
  if (!value) {
    return emptyWorkspaceValue();
  }
  return value;
}
