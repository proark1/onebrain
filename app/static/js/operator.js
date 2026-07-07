// Admin operator workspace: provisioning, spaces, deployments and rollout plans.

import {
  createRelease,
  getUpdatePlan,
  latestBackup,
  latestHealth,
  listDeploymentModules,
  listDeployments,
  listPlatformAccounts,
  listPlatformApps,
  listPlatformSpaces,
  listProvisioningBundles,
  listReleases,
  listRollouts,
  provisionCustomer,
  recordBackup,
  recordHealth,
  startRollout,
  updateRollout,
} from "./api.js";
import { el, qs, toast } from "./dom.js";

let bundles = [];
let releases = [];
let deploymentRows = [];
let loaded = false;

const text = (value) => value || "-";
const moduleCount = (bundle) => `${bundle.modules.length} module${bundle.modules.length === 1 ? "" : "s"}`;

function chip(label, tone = "") {
  return el("span", { class: `operator-chip ${tone}`.trim() }, label);
}

function emptyRow(message) {
  return el("div", { class: "operator-empty" }, message);
}

function setStatus(message, tone = "") {
  const node = qs("#provisionStatus");
  node.textContent = message;
  node.className = `operator-status ${tone}`.trim();
}

function populateBundleSelect() {
  const select = qs("#provisionBundle");
  const current = select.value || "full_stack";
  select.replaceChildren(...bundles.map((bundle) =>
    el("option", { value: bundle.id }, bundle.label)));
  if (bundles.some((bundle) => bundle.id === current)) select.value = current;
}

function renderBundles() {
  qs("#bundleCount").textContent = bundles.length;
  populateBundleSelect();

  const list = qs("#bundleList");
  if (!bundles.length) {
    list.replaceChildren(emptyRow("No bundles registered."));
    return;
  }

  list.replaceChildren(...bundles.map((bundle) =>
    el("article", { class: "bundle-item" },
      el("div", { class: "bundle-main" },
        el("strong", {}, bundle.label),
        el("span", {}, bundle.description)),
      el("div", { class: "operator-chip-row" },
        ...bundle.spaces.map((space) => chip(space)),
        chip(moduleCount(bundle), "muted")))));
}

async function loadAccounts() {
  const accounts = await listPlatformAccounts();
  qs("#accountCount").textContent = accounts.length;
  const rows = await Promise.all(accounts.map(async (account) => {
    const [spaces, apps] = await Promise.all([
      listPlatformSpaces(account.id),
      listPlatformApps(account.id),
    ]);
    return { account, spaces, apps };
  }));

  const list = qs("#accountList");
  if (!rows.length) {
    list.replaceChildren(emptyRow("No customer accounts yet."));
    return;
  }

  list.replaceChildren(...rows.map(({ account, spaces, apps }) =>
    el("article", { class: "operator-row" },
      el("div", { class: "operator-row-main" },
        el("div", { class: "operator-row-title" },
          el("strong", {}, account.name),
          el("code", {}, account.id)),
        el("div", { class: "operator-row-meta" }, `${account.kind} / owner ${text(account.owner_user_id)}`),
        el("div", { class: "operator-chip-row" },
          ...spaces.map((space) => chip(`${space.kind}: ${space.name}`)))),
      el("div", { class: "operator-row-side" },
        apps.length
          ? el("div", { class: "operator-app-list" },
            ...apps.map((app) => el("span", {}, `${app.app_id} / ${app.enabled_space_ids.length} spaces`)))
          : el("span", { class: "operator-muted" }, "No apps")))));
}

function renderReleaseRail() {
  const rail = qs("#releaseRail");
  if (!releases.length) {
    rail.replaceChildren(emptyRow("No release manifests registered."));
    return;
  }
  rail.replaceChildren(...releases.map((release) =>
    el("div", { class: "release-token", title: release.git_sha },
      el("strong", {}, release.version),
      el("span", {}, `${release.status} / ${Object.keys(release.modules).length} modules`))));
}

function selectedDeploymentRow() {
  const deploymentId = qs("#releaseSource").value;
  return deploymentRows.find((row) => row.deployment.id === deploymentId);
}

function updateReleaseModulePreview() {
  const preview = qs("#releaseModulePreview");
  const row = selectedDeploymentRow();
  if (!row) {
    preview.replaceChildren(emptyRow("Provision a deployment first."));
    return;
  }
  const target = qs("#releaseModuleVersion").value.trim() || qs("#releaseVersion").value.trim() || "target";
  preview.replaceChildren(
    el("div", { class: "operator-chip-row" },
      ...row.modules.map((module) => chip(`${module.module_id} -> ${target}`, "module"))),
  );
}

function populateReleaseSource() {
  const select = qs("#releaseSource");
  const current = select.value;
  select.replaceChildren(...deploymentRows.map((row) =>
    el("option", { value: row.deployment.id }, `${row.deployment.customer_name} / ${row.deployment.id}`)));
  select.disabled = deploymentRows.length === 0;
  if (deploymentRows.some((row) => row.deployment.id === current)) select.value = current;
  const row = selectedDeploymentRow();
  if (row && !qs("#releaseMigrationFrom").value.trim()) {
    qs("#releaseMigrationFrom").value = row.deployment.current_migration || "";
  }
  updateReleaseModulePreview();
}

function renderPlan(plan, resultNode, rolloutButton) {
  resultNode.replaceChildren(
    el("span", { class: plan.allowed ? "plan-ok" : "plan-blocked" }, plan.allowed ? "Allowed" : "Blocked"),
    el("span", {}, plan.reason.replaceAll("_", " ")),
  );
  rolloutButton.hidden = !(plan.allowed && Object.keys(plan.modules_to_update || {}).length);
}

function rolloutActions(rollout, deployment) {
  const actions = el("span", { class: "rollout-actions" });
  const canAdvance = !["success", "failed"].includes(rollout.status);
  if (!canAdvance) return actions;

  const makeButton = (label, status, tone = "") => {
    const button = el("button", { class: `rollout-action ${tone}`.trim(), type: "button" }, label);
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        await updateRollout(rollout.id, { status, notes: `Marked ${status} from operator dashboard.` });
        toast(`${deployment.customer_name} rollout marked ${status}`);
        await loadDeployments();
      } catch (err) {
        toast(err.message);
        button.disabled = false;
      }
    });
    return button;
  };

  actions.append(
    makeButton("Running", "running"),
    makeButton("Success", "success", "success"),
    makeButton("Failed", "failed", "danger"),
  );
  return actions;
}

async function loadDeployments() {
  const [deployments, releaseList] = await Promise.all([listDeployments(), listReleases()]);
  releases = releaseList;
  qs("#deploymentCount").textContent = deployments.length;
  renderReleaseRail();

  const rows = await Promise.all(deployments.map(async (deployment) => {
    const [modules, rollouts, backup, health] = await Promise.all([
      listDeploymentModules(deployment.id),
      listRollouts(deployment.id),
      latestBackup(deployment.id),
      latestHealth(deployment.id),
    ]);
    return { deployment, modules, rollouts, backup, health };
  }));
  deploymentRows = rows;
  populateReleaseSource();

  const list = qs("#deploymentList");
  if (!rows.length) {
    list.replaceChildren(emptyRow("No deployments tracked yet."));
    return;
  }

  list.replaceChildren(...rows.map(({ deployment, modules, rollouts, backup, health }) => {
    const releaseSelect = el("select", { class: "select operator-plan-select" },
      ...releases.map((release) => el("option", { value: release.version }, release.version)));
    releaseSelect.disabled = releases.length === 0;

    const planButton = el("button", { class: "mini-btn", type: "button" }, "Plan");
    planButton.disabled = releases.length === 0;
    const rolloutButton = el("button", { class: "mini-btn mini-btn-primary", type: "button", hidden: "" }, "Start rollout");
    const planResult = el("div", { class: "plan-result" });

    planButton.addEventListener("click", async () => {
      planButton.disabled = true;
      planResult.replaceChildren(el("span", {}, "Checking..."));
      try {
        const plan = await getUpdatePlan(deployment.id, releaseSelect.value);
        renderPlan(plan, planResult, rolloutButton);
      } catch (err) {
        planResult.replaceChildren(el("span", { class: "plan-blocked" }, err.message));
      } finally {
        planButton.disabled = false;
      }
    });

    rolloutButton.addEventListener("click", async () => {
      rolloutButton.disabled = true;
      try {
        await startRollout(deployment.id, releaseSelect.value);
        toast(`Rollout queued for ${deployment.customer_name}`);
        await loadDeployments();
      } catch (err) {
        toast(err.message);
        rolloutButton.disabled = false;
      }
    });

    const backupButton = el("button", { class: "mini-btn", type: "button" }, "Record backup");
    backupButton.addEventListener("click", async () => {
      backupButton.disabled = true;
      try {
        await recordBackup(deployment.id, { status: "success", detail: "Operator recorded pre-update backup." });
        toast(`Backup recorded for ${deployment.customer_name}`);
        await loadDeployments();
      } catch (err) {
        toast(err.message);
        backupButton.disabled = false;
      }
    });

    const healthButton = el("button", { class: "mini-btn", type: "button" }, "Record health");
    healthButton.addEventListener("click", async () => {
      healthButton.disabled = true;
      try {
        await recordHealth(deployment.id, { status: "success", detail: "Operator recorded health check." });
        toast(`Health recorded for ${deployment.customer_name}`);
        await loadDeployments();
      } catch (err) {
        toast(err.message);
        healthButton.disabled = false;
      }
    });

    return el("article", { class: "operator-row" },
      el("div", { class: "operator-row-main" },
        el("div", { class: "operator-row-title" },
          el("strong", {}, deployment.customer_name),
          el("code", {}, deployment.id)),
        el("div", { class: "operator-row-meta" },
          `${deployment.deployment_type} / ${deployment.release_ring} / version ${text(deployment.current_version)}`),
        el("div", { class: "readiness-list" },
          el("span", { class: `readiness-chip ${backup?.status || ""}`.trim() },
            `backup ${backup?.status || "none"}`),
          el("span", { class: `readiness-chip ${health?.status || ""}`.trim() },
            `health ${health?.status || "none"}`)),
        el("div", { class: "operator-chip-row" },
          ...modules.map((module) => chip(`${module.module_id} ${module.version}`, "module")))),
      el("div", { class: "operator-plan" },
        releaseSelect,
        el("div", { class: "operator-plan-actions" }, planButton, rolloutButton, backupButton, healthButton),
        planResult,
        el("div", { class: "rollout-list" },
          ...(rollouts.length
            ? rollouts.slice(-3).reverse().map((rollout) =>
              el("div", { class: "rollout-line" },
                el("span", {}, `${rollout.target_version} / ${rollout.status}`),
                rolloutActions(rollout, deployment)))
            : [el("span", { class: "operator-muted" }, "No rollouts")]))));
  }));
}

async function loadOperator() {
  setStatus("Ready");
  try {
    [bundles] = await Promise.all([listProvisioningBundles()]);
    renderBundles();
    await Promise.all([loadAccounts(), loadDeployments()]);
    loaded = true;
  } catch (err) {
    toast(err.message);
  }
}

async function submitProvisionForm(event) {
  event.preventDefault();
  const submit = qs("#provisionSubmit");
  submit.disabled = true;
  setStatus("Provisioning", "busy");
  try {
    const payload = {
      customer_name: qs("#provisionName").value.trim(),
      bundle_id: qs("#provisionBundle").value,
      initial_version: qs("#provisionVersion").value.trim(),
      release_ring: qs("#provisionRing").value,
      deployment_type: qs("#provisionType").value,
      region: qs("#provisionRegion").value.trim(),
    };
    const accountId = qs("#provisionAccount").value.trim();
    if (accountId) payload.account_id = accountId;

    const result = await provisionCustomer(payload);
    toast(`Provisioned ${result.account.name}`);
    qs("#provisionForm").reset();
    qs("#provisionBundle").value = payload.bundle_id;
    qs("#provisionVersion").value = payload.initial_version;
    qs("#provisionRing").value = payload.release_ring;
    qs("#provisionType").value = payload.deployment_type;
    setStatus("Created", "ok");
    await loadOperator();
  } catch (err) {
    setStatus("Blocked", "error");
    toast(err.message);
  } finally {
    submit.disabled = false;
  }
}

function setReleaseStatus(message, tone = "") {
  const node = qs("#releaseStatus");
  node.textContent = message;
  node.className = `operator-status ${tone}`.trim();
}

async function submitReleaseForm(event) {
  event.preventDefault();
  const row = selectedDeploymentRow();
  if (!row) {
    toast("Select a source deployment first.");
    return;
  }
  const submit = qs("#releaseSubmit");
  const releaseVersion = qs("#releaseVersion").value.trim();
  const moduleVersion = qs("#releaseModuleVersion").value.trim() || releaseVersion;
  submit.disabled = true;
  setReleaseStatus("Creating", "busy");
  try {
    const modules = Object.fromEntries(row.modules.map((module) => [module.module_id, moduleVersion]));
    const release = await createRelease({
      version: releaseVersion,
      git_sha: qs("#releaseGitSha").value.trim(),
      status: qs("#releaseManifestStatus").value,
      migration_from: qs("#releaseMigrationFrom").value.trim(),
      migration_to: qs("#releaseMigrationTo").value.trim(),
      security_notes: qs("#releaseSecurityNotes").value.trim(),
      rollback_plan: qs("#releaseRollbackPlan").value.trim(),
      modules,
    });
    toast(`Release ${release.version} created`);
    qs("#releaseForm").reset();
    setReleaseStatus("Created", "ok");
    await loadDeployments();
  } catch (err) {
    setReleaseStatus("Blocked", "error");
    toast(err.message);
  } finally {
    submit.disabled = false;
    updateReleaseModulePreview();
  }
}

export function initOperator(me) {
  const button = qs("#operatorBtn");
  const chatView = qs("#chatView");
  const operatorView = qs("#operatorView");

  if (me.role_id !== "admin") {
    button.hidden = true;
    return { showChat: () => {} };
  }

  button.hidden = false;

  const showChat = () => {
    operatorView.hidden = true;
    chatView.hidden = false;
    button.classList.remove("active");
  };

  const showOperator = async () => {
    chatView.hidden = true;
    operatorView.hidden = false;
    button.classList.add("active");
    if (!loaded) await loadOperator();
  };

  button.addEventListener("click", () => {
    if (operatorView.hidden) showOperator();
    else showChat();
  });
  qs("#operatorRefresh").addEventListener("click", loadOperator);
  qs("#provisionForm").addEventListener("submit", submitProvisionForm);
  qs("#releaseForm").addEventListener("submit", submitReleaseForm);
  qs("#releaseSource").addEventListener("change", () => {
    const row = selectedDeploymentRow();
    if (row) qs("#releaseMigrationFrom").value = row.deployment.current_migration || "";
    updateReleaseModulePreview();
  });
  qs("#releaseVersion").addEventListener("input", updateReleaseModulePreview);
  qs("#releaseModuleVersion").addEventListener("input", updateReleaseModulePreview);

  return { showChat, showOperator };
}
