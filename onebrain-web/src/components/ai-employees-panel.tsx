"use client";

import dynamic from "next/dynamic";
import { useCallback, useEffect, useMemo, useState } from "react";
import { AiEmployeeActions } from "@/components/ai-employee-actions";
import { AiEmployeeChat } from "@/components/ai-employee-chat";
import { AiEmployeeConnectors } from "@/components/ai-employee-connectors";
import { AiEmployeeOrganization } from "@/components/ai-employee-organization";
import { AiEmployeeWork } from "@/components/ai-employee-work";
import { useWorkspace } from "@/components/workspace-provider";
import {
  getAiConnectorHealth,
  getAiEmployeeTeam,
  getAiModels,
  listAiActions,
  listAiConnectors,
  listAiConversations,
  listAiEmployeeWorkspaces,
  listAiMissions,
  listAiWorkProducts,
} from "@/lib/onebrain-client";
import type {
  AiActionProposal,
  AiConnectorBinding,
  AiConnectorHealth,
  AiEmployeeConversation,
  AiEmployeeTeam,
  AiEmployeeWorkspace,
  AiMission,
  AiModels,
  AiWorkProduct,
} from "@/lib/onebrain-types";

const AiEmployeeMissions = dynamic(
  () => import("@/components/ai-employee-missions").then((module) => module.AiEmployeeMissions),
  { loading: () => <ModuleLoading label="Opening mission room" /> },
);
const AiEmployeeAdmin = dynamic(
  () => import("@/components/ai-employee-admin").then((module) => module.AiEmployeeAdmin),
  { loading: () => <ModuleLoading label="Opening character studio" /> },
);

type Tab = "organization" | "chats" | "missions" | "work" | "actions" | "connectors" | "admin";

const EMPTY_MODELS: AiModels = { health: [], policies: [] };

function ModuleLoading({ label }: { label: string }) {
  return <div className="aiEmptyPanel"><span>LOADING</span><h2>{label}</h2></div>;
}

export function AiEmployeesPanel() {
  const workspace = useWorkspace();
  const [tab, setTab] = useState<Tab>("organization");
  const [workspaces, setWorkspaces] = useState<AiEmployeeWorkspace[]>([]);
  const [team, setTeam] = useState<AiEmployeeTeam | null>(null);
  const [conversations, setConversations] = useState<AiEmployeeConversation[]>([]);
  const [missions, setMissions] = useState<AiMission[]>([]);
  const [work, setWork] = useState<AiWorkProduct[]>([]);
  const [actions, setActions] = useState<AiActionProposal[]>([]);
  const [bindings, setBindings] = useState<AiConnectorBinding[]>([]);
  const [connectorHealth, setConnectorHealth] = useState<AiConnectorHealth[]>([]);
  const [models, setModels] = useState<AiModels>(EMPTY_MODELS);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    void listAiEmployeeWorkspaces()
      .then((rows) => {
        if (!active) return;
        setWorkspaces(rows);
        if (!rows.length) setLoading(false);
      })
      .catch((reason: Error) => {
        if (!active) return;
        setError(reason.message);
        setLoading(false);
      });
    return () => { active = false; };
  }, []);

  const activeWorkspace = useMemo(() => {
    const selected = workspaces.find((row) => (
      row.account_id === workspace.selectedAccountId && row.space_id === workspace.selectedSpaceId
    ));
    return selected ?? workspaces[0] ?? null;
  }, [workspace.selectedAccountId, workspace.selectedSpaceId, workspaces]);
  const accountId = activeWorkspace?.account_id ?? "";
  const spaceId = activeWorkspace?.space_id ?? "";

  const refreshTeam = useCallback(async () => {
    if (!accountId || !spaceId) return;
    const [nextTeam, nextModels] = await Promise.all([
      getAiEmployeeTeam(accountId, spaceId),
      getAiModels(accountId, spaceId),
    ]);
    setTeam(nextTeam);
    setModels(nextModels);
  }, [accountId, spaceId]);
  const refreshConversations = useCallback(async () => {
    if (accountId && spaceId) setConversations(await listAiConversations(accountId, spaceId));
  }, [accountId, spaceId]);
  const refreshMissions = useCallback(async () => {
    if (accountId && spaceId) setMissions(await listAiMissions(accountId, spaceId));
  }, [accountId, spaceId]);
  const refreshActions = useCallback(async () => {
    if (accountId && spaceId) setActions(await listAiActions(accountId, spaceId));
  }, [accountId, spaceId]);
  const refreshConnectors = useCallback(async () => {
    if (!accountId || !spaceId) return;
    const [nextBindings, nextHealth] = await Promise.all([
      listAiConnectors(accountId, spaceId),
      getAiConnectorHealth(accountId, spaceId),
    ]);
    setBindings(nextBindings);
    setConnectorHealth(nextHealth);
  }, [accountId, spaceId]);

  useEffect(() => {
    let active = true;
    if (!accountId || !spaceId) return () => { active = false; };
    void Promise.all([
      getAiEmployeeTeam(accountId, spaceId),
      listAiConversations(accountId, spaceId),
      listAiMissions(accountId, spaceId),
      listAiWorkProducts(accountId, spaceId),
      listAiActions(accountId, spaceId),
      listAiConnectors(accountId, spaceId),
      getAiConnectorHealth(accountId, spaceId),
      getAiModels(accountId, spaceId),
    ]).then(([nextTeam, nextConversations, nextMissions, nextWork, nextActions, nextBindings, nextHealth, nextModels]) => {
      if (!active) return;
      setTeam(nextTeam);
      setConversations(nextConversations);
      setMissions(nextMissions);
      setWork(nextWork);
      setActions(nextActions);
      setBindings(nextBindings);
      setConnectorHealth(nextHealth);
      setModels(nextModels);
    }).catch((reason: Error) => {
      if (active) setError(reason.message);
    }).finally(() => {
      if (active) setLoading(false);
    });
    return () => { active = false; };
  }, [accountId, spaceId]);

  const pendingActions = actions.filter((action) => action.status === "proposed").length;
  const activeMissions = missions.filter((mission) => ["draft", "queued", "running", "paused"].includes(mission.status)).length;
  const tabs: { id: Tab; label: string; count?: number }[] = [
    { id: "organization", label: "Organization", count: team?.agents.length },
    { id: "chats", label: "Chats", count: conversations.length },
    { id: "missions", label: "Missions", count: activeMissions },
    { id: "work", label: "Work", count: work.length },
    { id: "actions", label: "Approvals", count: pendingActions },
    { id: "connectors", label: "Connectors", count: bindings.filter((row) => row.status === "active").length },
    { id: "admin", label: "Admin" },
  ];

  if (loading && !team) return <ModuleLoading label="Assembling the AI team" />;
  if (!activeWorkspace) return <div className="aiEmptyPanel"><span>MODULE</span><h2>AI Employees is not installed in an accessible workspace</h2><p>Install the optional AI Employees app in a business or shared space to bring the team live.</p></div>;
  if (!team) return <div className="aiEmptyPanel"><span>OFFLINE</span><h2>The AI Employees module could not load</h2><p>{error || "Check the API and module installation, then try again."}</p></div>;

  return (
    <div className="aiEmployeesWorkspace">
      <header className="aiModuleHeader">
        <div>
          <span className="eyebrow">AI Employees · {activeWorkspace.space_name}</span>
          <h1>Your company, assembled.</h1>
          <p>Sixteen persistent specialists. Separate judgment. One governed operating system.</p>
        </div>
        <div className="aiModulePosture">
          <span className={`aiLiveDot ${team.installation_status}`} />
          <div><strong>{team.installation_status === "active" ? "Team live" : "Module paused"}</strong><small>{team.contract_version} · Gemini default</small></div>
        </div>
      </header>

      <div className="aiPulseStrip" aria-label="AI employee module status">
        <div><span>Team</span><strong>{team.agents.filter((agent) => agent.status === "active").length}/{team.agents.length}</strong><small>active employees</small></div>
        <div><span>Mission rule</span><strong>≤ {team.max_mission_squad_size}</strong><small>people per squad</small></div>
        <div><span>In motion</span><strong>{activeMissions}</strong><small>open missions</small></div>
        <div className={pendingActions ? "attention" : ""}><span>Human queue</span><strong>{pendingActions}</strong><small>actions to review</small></div>
        <div><span>Connected</span><strong>{bindings.filter((row) => row.status === "active").length}</strong><small>governed tools</small></div>
      </div>

      {team.installation_status !== "active" ? <p className="notice warning">The module is paused. History remains readable; chats, missions, configuration, and actions stay locked.</p> : null}
      {error ? <p className="inlineError">{error}</p> : null}

      <nav className="aiModuleTabs" aria-label="AI Employees sections">
        {tabs.map((item) => <button aria-current={tab === item.id ? "page" : undefined} className={tab === item.id ? "active" : ""} key={item.id} onClick={() => setTab(item.id)} type="button"><span>{item.label}</span>{typeof item.count === "number" ? <small>{item.count}</small> : null}</button>)}
      </nav>

      <div className="aiModuleCanvas">
        {tab === "organization" ? <AiEmployeeOrganization team={team} /> : null}
        {tab === "chats" ? <AiEmployeeChat accountId={accountId} agents={team.agents} conversations={conversations} onConversationsChanged={refreshConversations} spaceId={spaceId} /> : null}
        {tab === "missions" ? <AiEmployeeMissions accountId={accountId} agents={team.agents} maxSquadSize={team.max_mission_squad_size} missions={missions} onMissionsChanged={refreshMissions} spaceId={spaceId} /> : null}
        {tab === "work" ? <AiEmployeeWork agents={team.agents} work={work} /> : null}
        {tab === "actions" ? <AiEmployeeActions accountId={accountId} actions={actions} agents={team.agents} onActionsChanged={refreshActions} spaceId={spaceId} /> : null}
        {tab === "connectors" ? <AiEmployeeConnectors accountId={accountId} agents={team.agents} bindings={bindings} canManage={team.can_manage_connectors} health={connectorHealth} onConnectorsChanged={refreshConnectors} spaceId={spaceId} /> : null}
        {tab === "admin" ? team.can_configure ? <AiEmployeeAdmin accountId={accountId} agents={team.agents} models={models} onTeamChanged={refreshTeam} spaceId={spaceId} /> : <div className="aiEmptyPanel"><span>ADMIN</span><h2>Project admin access required</h2><p>Character prompts, versions, model policies, and employee status are immutable to ordinary members.</p></div> : null}
      </div>
    </div>
  );
}
