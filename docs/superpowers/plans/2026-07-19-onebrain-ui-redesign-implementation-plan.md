# OneBrain interface redesign implementation plan

**Date:** 2026-07-19

**Design:** `docs/superpowers/specs/2026-07-19-onebrain-ui-redesign-design.md`

## Objective

Apply the approved balanced-workspace design to every `onebrain-web` surface
without changing API contracts, authorization, or feature behavior. The work
must preserve OneBrain's dark/copper identity while making page hierarchy,
spacing, state, responsive behavior, forms, and operational data consistent.

## Delivery constraints

- Work in an isolated `codex/onebrain-ui-redesign` worktree because the current
  worktree contains unrelated user changes and Drive work.
- Start the implementation branch from local `main`, then cherry-pick the
  approved design and implementation-plan commits. Do not carry unrelated
  `codex/onebrain-drive` commits into the UI branch.
- Do not change backend endpoints, OpenAPI contracts, session capabilities, or
  navigation authorization.
- Do not add a component framework, icon package, remote font, or runtime font
  download.
- Preserve every existing operational field. Progressive disclosure may change
  where a field appears, never whether it is available.
- Keep the application buildable after each checkpoint and commit only files
  related to the UI redesign.
- Treat the current feature-specific layouts as domain behavior. Consolidate
  their visual language without flattening Ask, Drive, and AI Employees into
  generic dashboard cards.

## Implementation order

### 1. Capture a code and visual baseline

**Files**

- Inspect all routes under `onebrain-web/src/app/`.
- Inspect shared styles in `onebrain-web/src/app/globals.css` and the Drive
  module stylesheet.
- Add or update contract tests in `onebrain-web/tests/console-surface.test.mjs`
  only where the approved shell requires a new invariant.

**Verify first**

- Run `npm test`, `npm run lint`, `npm run typecheck`, and `npm run build` from
  `onebrain-web` and record any pre-existing failure separately.
- Start the existing UI against the available local/dev configuration.
- Capture representative baseline screenshots at 1440px, 1024px, and 390px for
  Fleet, Status, Ask, Drive, AI Employees, and Settings when authentication and
  data are available.
- Record which routes cannot be rendered locally and use source inspection plus
  post-build browser verification for them.

**Outcome**

- A known-good behavioral baseline and an explicit list of visual regressions
  to avoid.

### 2. Establish the token and foundation layer

**Files**

- Modify `onebrain-web/src/app/globals.css`.
- Modify `onebrain-web/src/app/layout.tsx` only if document metadata or root
  classes need alignment.
- Modify `onebrain-web/src/features/drive/drive.module.css` where hard-coded
  values conflict with the shared tokens.

**Implement**

- Replace the existing near-duplicate brand variables with the approved token
  set for ink, copper, canvas, surfaces, text, muted text, borders, and semantic
  state colors.
- Define the approved typography roles, spacing scale, radii, control heights,
  focus ring, and limited shadow rules at the top of the stylesheet.
- Normalize body copy, headings, buttons, inputs, selects, textareas, links,
  code, tables, and reduced-motion behavior.
- Make semantic tints and text colors available through named variables so
  feature styles never invent another green, amber, or red.
- Preserve the local system font and monospace stacks.
- Remove superseded variables and declarations as they migrate; do not append a
  second override system at the end of the file.

**Verify**

- Search for legacy token literals and classify each remaining use as
  feature-specific or migrate it.
- Verify focus visibility, disabled controls, high-contrast semantic labels,
  and reduced motion in a minimal rendered route.

### 3. Rebuild the responsive application shell

**Files**

- Modify `onebrain-web/src/components/console-shell.tsx`.
- Add `onebrain-web/src/components/console-navigation.tsx` if client-side
  drawer state is required.
- Modify `onebrain-web/src/lib/console-navigation.ts` without changing the
  authorized destination sets.
- Modify `onebrain-web/src/app/globals.css`.
- Update `onebrain-web/tests/console-surface.test.mjs`.

**Test first**

- Mission Control still exposes only Status, Control, Fleet, and Settings.
- Customer deployments still expose the complete current customer surface and
  no control-plane links.
- Navigation grouping metadata never changes the flattened destination order.
- Active links expose `aria-current="page"`.
- The mobile trigger has an accessible name and controls a labeled navigation
  region.
- Workspace selection remains present on the same eligible surfaces.

**Implement**

- Build the 208px desktop sidebar, grouped navigation labels, compact 36px
  destination rows, copper active signal rail, bottom Settings placement, and
  optional reliable system-status footer.
- Replace duplicated top-bar identity with compact location context and one
  account control.
- Collapse to a 72px icon rail at tablet widths. Use small repository-native
  SVG components or CSS shapes with accessible labels; do not add an icon
  dependency.
- Add a keyboard-operable mobile menu and modal drawer below 768px, including
  focus management, escape close, backdrop close, scroll locking, and focus
  restoration.
- Keep shell structure server-rendered; isolate only drawer interaction in a
  small client component.
- Preserve password-change redirect and workspace-provider boundaries.

**Verify**

- Keyboard-test every navigation item and the mobile drawer.
- Check 200% browser zoom and 320px minimum width.
- Confirm no content shifts underneath the sticky top bar.

### 4. Upgrade the shared presentation primitives

**Files**

- Modify `onebrain-web/src/components/admin-ui.tsx`.
- Modify `onebrain-web/src/components/operational/status-summary.tsx`.
- Modify `onebrain-web/src/components/operational/timestamp.tsx`.
- Modify `onebrain-web/src/components/operational/expandable-card.tsx`.
- Add focused primitives under `onebrain-web/src/components/ui/` only when an
  existing component cannot own the responsibility cleanly.
- Modify `onebrain-web/src/app/globals.css`.
- Add Node-based static/contract tests under `onebrain-web/tests/` for semantic
  output that can be verified without adding a browser test framework.

**Test first**

- `PageHeader` accepts one description and renders actions separately.
- Tabs expose tab roles, selected state, labels, and counts consistently.
- Status summaries require condition and explanation; timestamps remain real
  `time` elements.
- Notices use `status` for routine outcomes and `alert` for errors.
- Disclosure controls expose `aria-expanded` and a stable target relationship.
- Empty and error states include an actionable title and optional action.

**Implement**

- Add the approved `description` contract to `PageHeader` and remove page-level
  subtitle workarounds as routes migrate.
- Redesign tabs as an underline row without an enclosing floating card.
- Redesign status summaries with the semantic signal rail and compact counts.
- Make `Panel`/section surfaces flat, bordered, and functional rather than
  decorative.
- Standardize labeled status badges, notices, field help/errors, loading
  skeletons, empty states, and retry states.
- Add a reusable disclosure pattern for grouped operational rows without
  forcing one generic table abstraction on every feature.

**Verify**

- Render each primitive in success, warning, danger, neutral, loading, empty,
  and error states.
- Confirm long text, long IDs, and translated-length labels wrap safely.

### 5. Make Fleet the reference operational screen

**Files**

- Modify `onebrain-web/src/components/fleet-panel.tsx`.
- Add `onebrain-web/src/components/fleet/deployment-row.tsx` if separating row
  disclosure keeps the panel focused.
- Modify `onebrain-web/src/app/globals.css`.
- Add `onebrain-web/tests/fleet-presentation.test.mjs` for pure grouping and
  status helpers if those helpers move to a testable module.

**Test first**

- Health status maps `true`, `false`, and no signal to explicit labels and
  semantic tones.
- A deployment presentation includes every current field: identifiers,
  customer/gate context, ring, reported and registry versions, created time,
  active-since time, report and receipt times, users, storage, and alerts.
- Summary status distinguishes all healthy, alerts, unhealthy, and missing
  signal cases.
- Disclosure controls are keyboard operable and retain exact timestamps.
- Rollout and enrollment mutations preserve their current request payloads and
  success/error behavior.

**Implement**

- Use Fleet as the exact implementation of the approved monitor/configure
  pattern: task-based page header, underline tabs, one decision summary, and a
  grouped deployment list.
- Group the desktop columns into Health, Deployment, Release, Activity, Usage,
  and Alerts. Keep primary rows 52–56px high.
- Move exact timestamps, registry mismatch explanation, storage detail, and
  other secondary fields into an expandable detail region.
- Convert each row to a labeled record below 768px rather than relying on a
  wide horizontal scroll.
- Rework Rollouts into a readable form section plus history and keep state
  actions semantically distinct.
- Rework Enrollment keys with the same form/table conventions and preserve the
  one-time secret warning.

**Verify**

- Compare against the approved Fleet mock at all three target widths.
- Verify empty, unavailable control plane, API error, refresh, one-time token,
  alert, version mismatch, and no-signal states.

### 6. Apply the monitor pattern to operational surfaces

**Files**

- Modify `onebrain-web/src/components/cockpit-panel.tsx`.
- Refactor `onebrain-web/src/components/operator-panel.tsx` only where needed
  to create clear monitoring and configuration sections.
- Modify `onebrain-web/src/components/kpi-panel.tsx`.
- Modify `onebrain-web/src/components/privacy-panel.tsx`.
- Modify supporting components under
  `onebrain-web/src/components/operational/`.
- Modify `onebrain-web/src/app/globals.css`.

**Implement**

- Give each route one page identity and one decision-first summary.
- Replace unnecessary metric-card walls with compact counts that support the
  decision summary.
- Group related status signals, timestamps, and actions into scan-friendly
  rows or expandable sections.
- Keep raw diagnostic and provider detail available behind disclosure.
- Separate Control provisioning/configuration forms from monitoring history
  using the configure pattern.
- Preserve all current fetch concurrency and mutation behavior.

**Verify**

- Check representative healthy, warning, failed, loading, and empty states.
- Confirm no status is inferred as healthy from missing telemetry.
- Compare all four screens for identical heading, tab, status, badge, and
  section rhythm.

### 7. Apply the focused-workspace system

**Files**

- Modify `onebrain-web/src/components/chat-panel.tsx`.
- Modify `onebrain-web/src/features/drive/drive-app.tsx` and focused Drive
  presentation components only where shared structure is required.
- Modify `onebrain-web/src/features/drive/drive.module.css`.
- Modify `onebrain-web/src/components/documents-panel.tsx`.
- Modify `onebrain-web/src/components/ai-employees-panel.tsx` and its visual
  subcomponents.
- Modify `onebrain-web/src/app/globals.css`.

**Implement**

- Give Ask, Drive, Documents, and AI Employees a shared full-height workspace
  rhythm: compact task toolbar, optional contextual rail, primary canvas, and
  stable action area.
- Remove duplicated page/module headings and redundant card shells.
- Align rails, tabs, empty states, toolbars, fields, buttons, dialogs, badges,
  and focus behavior with the foundation tokens.
- Preserve Drive's task-specific file browser and AI Employees' module-specific
  organization views; visual consistency must not erase useful information
  architecture.
- Ensure long conversations, file lists, and employee work areas scroll inside
  their intended region rather than expanding the whole shell unpredictably.

**Verify**

- Test rail collapse, content overflow, composers/toolbars, dialogs, empty
  folders, no conversations, paused AI module, and API failure states.
- Check mobile height behavior with the virtual keyboard-safe viewport units
  supported by the current browser targets.

### 8. Apply the configure and authentication patterns

**Files**

- Modify `onebrain-web/src/components/settings-panel.tsx`.
- Modify `onebrain-web/src/components/password-change-panel.tsx`.
- Modify `onebrain-web/src/app/settings/password/page.tsx` so signed-in password
  management uses `ConsoleShell`.
- Modify `onebrain-web/src/components/spaces-panel.tsx`.
- Modify forms inside operator, Fleet, Privacy, and AI Employee admin surfaces.
- Modify `onebrain-web/src/components/login-panel.tsx` and
  `onebrain-web/src/components/app-state.tsx`.
- Modify `onebrain-web/src/app/globals.css`.

**Test first**

- Signed-in Settings and password routes use the shell.
- Password-change redirect behavior remains unchanged.
- Login and signed-out/API-unavailable states do not render authenticated
  navigation.
- Field labels, descriptions, errors, and action names remain accessible.

**Implement**

- Use readable form sections no wider than 720px.
- Keep labels persistent and place help or validation beside the related field.
- Group save/cancel actions and separate logout, revoke, delete, abort, and
  other destructive actions.
- Align dialogs and confirmations with the shared overlay behavior.
- Restyle login and exceptional state screens with the same tokens without
  forcing them into the authenticated shell.

**Verify**

- Keyboard-test every form, error path, confirmation, and password redirect.
- Verify action/success vocabulary pairs exactly.

### 9. Remove obsolete styles and complete accessibility

**Files**

- Modify `onebrain-web/src/app/globals.css`.
- Modify affected feature stylesheets and components discovered by the style
  audit.

**Implement**

- Remove obsolete selectors and competing definitions for migrated surfaces.
- Consolidate duplicate responsive breakpoints and semantic color literals.
- Ensure no migrated component depends on selector-order accidents.
- Add `prefers-reduced-motion` coverage, forced wrapping/ellipsis rules, touch
  target sizing, and 200% zoom resilience.
- Verify headings follow a logical order and landmarks are not duplicated.

**Verify**

- Run a selector/class usage audit for obviously dead shared styles.
- Search for focus suppression, inaccessible color-only state, unlabeled icon
  buttons, and tables without usable mobile labels.

### 10. Run the complete release gate

**Automated checks**

From `onebrain-web`:

1. `npm test`
2. `npm run lint`
3. `npm run typecheck`
4. `npm run build`

Run relevant repository tests if a shared route or server-rendering contract
outside `onebrain-web` changes.

**Visual checks**

At 1440px, 1024px, and 390px, verify:

- Fleet;
- Status;
- Ask;
- Drive;
- AI Employees; and
- Settings.

For each representative screen, inspect:

- default populated state;
- loading stability;
- empty state;
- recoverable error;
- long labels/data;
- keyboard focus;
- mobile navigation and disclosure; and
- reduced motion.

**Final review**

- Compare implementation against every completion criterion in the approved
  design.
- Confirm no backend/API/auth behavior changed.
- Confirm staged files contain only the UI redesign.
- Scan the staged diff for secrets and generated build output.
- Commit the implementation with a concise message.
- Push and merge according to `AGENTS.md` only if the isolated worktree is clean,
  every check passes, and the branch contains no unrelated changes.

## Completion criteria

Implementation is complete only when all ten steps are finished, the full
automated gate passes, the six representative surfaces pass visual inspection
at all three widths, and the shipped diff contains no unrelated files.
