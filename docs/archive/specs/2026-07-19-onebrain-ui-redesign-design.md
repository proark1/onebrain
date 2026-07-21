# OneBrain interface redesign

**Date:** 2026-07-19

**Status:** Approved design; implementation not started

**Scope:** The complete `onebrain-web` interface, including Mission Control and customer surfaces

## 1. Objective

Redesign OneBrain as a minimal, structured workspace without removing its
existing identity. The dark navigation, copper accent, and OneBrain wordmark
remain recognizable. The change is primarily about hierarchy, spacing,
information grouping, and predictable interaction.

The redesign must solve the problems visible in the current Fleet screen across
the whole product:

- oversized and undersized text do not form a useful hierarchy;
- navigation consumes space without grouping related destinations;
- the top bar and page header repeat the same identity;
- large active backgrounds and nested cards add weight without meaning;
- operational tables give every field equal priority, making rows difficult to
  scan;
- timestamps, status, user counts, storage, and settings use inconsistent
  layouts; and
- desktop layouts do not define a deliberate transformation for small screens.

Success means a user can answer three questions quickly on every screen:

1. Where am I?
2. What is the current state?
3. What can or should I do next?

## 2. Approved direction

The approved direction is the **balanced workspace**. It keeps enough density
for an operational console while using clear grouping and progressive
disclosure so secondary detail does not compete with decisions.

The rejected alternatives were:

- a compact operator console, which exposed the most information but remained
  visually busy; and
- spacious cards, which were calm but required too much scrolling and made
  comparison slower.

The redesign is not a new brand, a decorative dashboard, or a collection of
independent page makeovers. It is one system applied to three task-based page
patterns.

## 3. Design principles

### 3.1 One page identity

The shell identifies the product and location. The page header identifies the
task and explains it. A destination name must not appear as multiple competing
headlines.

### 3.2 Decisions before detail

Operational screens begin with a plain-language state summary such as “All
deployments are healthy” or “One signal needs attention.” Counts support that
decision; they do not replace it. Detailed tables or lists follow.

### 3.3 Structure instead of decoration

Spacing, dividers, labels, and grouping communicate relationships. Card
backgrounds and shadows are used only when a surface is functionally distinct.
Cards are never nested merely to create visual depth.

### 3.4 Consistency without forced uniformity

All screens share tokens, controls, status language, focus treatment, and type
hierarchy. Layout changes only when the task changes: monitoring, focused work,
or configuration.

### 3.5 One memorable device

The signature element is the **signal rail**: a slim two- or three-pixel
vertical rail used for active navigation and summary state. It is copper for
selection and semantic green, amber, or red for system state. It replaces
large tinted blocks and encodes real meaning.

## 4. Visual system

### 4.1 Color tokens

| Token | Value | Use |
|---|---:|---|
| Ink | `#171A1F` | Sidebar, primary buttons, strongest text |
| Ink hover | `#24282F` | Dark interactive hover state |
| Copper | `#AD742F` | OneBrain identity, active rail, restrained emphasis |
| Copper dark | `#87551F` | Accessible copper text and hover state |
| Canvas | `#F5F4F1` | Application background |
| Surface | `#FFFFFF` | Primary content surfaces |
| Subdued surface | `#FAF9F7` | Table headers and quiet grouped regions |
| Text | `#20242A` | Default foreground |
| Muted | `#6C7078` | Supporting copy and utility labels |
| Line | `#DEDDD8` | Hairline borders and dividers |
| Success | `#26724D` | Healthy and completed states |
| Success tint | `#E8F4ED` | Success badge background |
| Warning | `#9A671F` | Review and degraded states |
| Warning tint | `#F5EDDF` | Warning badge background |
| Danger | `#A3453F` | Failed and destructive states |
| Danger tint | `#F6E8E6` | Danger badge background |

Copper remains an identity color, not a general status color. Status uses the
semantic tokens. All text/background pairs must meet WCAG AA contrast.

### 4.2 Typography

The first implementation retains a local system-sans stack to avoid a font
download, layout shift, or build-time network dependency. Data identifiers and
versions use the existing system monospace stack. Typography is made distinct
through a disciplined scale rather than extreme size differences.

| Role | Size / line height | Weight |
|---|---|---:|
| Page title | `28px / 1.15` | 750 |
| Section title | `16px / 1.3` | 720 |
| Subsection title | `14px / 1.35` | 700 |
| Body and controls | `13px / 1.5` | 400–680 |
| Utility and metadata | `11px / 1.4` | 600–760 |
| Eyebrow | `10px / 1.3`, uppercase, `0.08em` tracking | 780 |

Sentence case is the default. Uppercase is restricted to short utility labels
and eyebrows. Weight and spacing establish hierarchy; headings do not jump from
very large to very small.

### 4.3 Spacing and shape

The spacing scale is `4, 8, 12, 16, 24, 32, 40` pixels.

- page gutters: 32px desktop, 24px tablet, 16px mobile;
- major section gap: 24px;
- surface padding: 16px, or 20px for a large form section;
- control height: 36px minimum;
- standard data row: 52–56px;
- control radius: 7px;
- surface radius: 9px;
- pill radius: 999px.

Static content surfaces use a hairline border and no shadow. A subtle shadow is
reserved for floating or overlay UI such as dialogs, menus, and the mobile
navigation drawer.

The existing OneBrain wordmark, dark/copper identity, and current mark content
remain. This work does not invent or replace the logo asset.

## 5. Application shell

### 5.1 Desktop

At widths of 1200px and above, the shell uses a 208px dark sidebar and a 56px
top bar.

The sidebar contains:

- the existing OneBrain brand at the top;
- destinations grouped under short labels such as Monitor, Work, Manage, and
  Account when the current surface has enough destinations to need groups;
- a 36px navigation row with an icon slot, label, and signal rail for the
  active destination;
- Settings near the bottom instead of separated by arbitrary empty space; and
- an optional system-status line in the footer when a reliable status is
  available.

Mission Control continues to expose only Status, Control, Fleet, and Settings.
Customer deployments continue to expose their existing customer navigation.
The redesign does not alter capability-based navigation rules.

The top bar contains a breadcrumb-like context on the left and one compact
identity control on the right. Workspace selection remains available where the
existing shell requires it. It must not repeat the page title as a second large
heading.

### 5.2 Main content

Operational content may use the available width up to a 1600px maximum. Reading
and form surfaces use narrower measures defined by their page pattern. The page
header contains:

- one eyebrow identifying the domain;
- one page title;
- one sentence describing the job of the page; and
- page-level actions aligned to the right.

Tabs are an underline navigation row with a copper active indicator. They are
not enclosed in another floating card.

### 5.3 Settings and signed-in account screens

Signed-in Settings and password management use the same console shell so users
do not appear to leave the product. Login and other signed-out states retain a
dedicated authentication layout using the same tokens and brand.

## 6. Page patterns

### 6.1 Monitor and compare

Used by Status, Control, Fleet, KPIs, Privacy monitoring, and other operational
tables.

The order is:

1. page header and action;
2. section tabs when needed;
3. decision summary with semantic signal rail;
4. a small number of supporting counts;
5. grouped table or list; and
6. expandable detail for secondary metadata.

Metric tiles are not the default opening element. They appear only when each
number has a distinct decision-making purpose.

### 6.2 Focused workspace

Used by Ask, Drive, Documents, and AI Employees.

These screens use the available viewport height. A compact toolbar anchors the
current task. A contextual rail appears only when it improves selection, such
as conversations or folders. Content and composition receive the majority of
space. Tool-specific chrome may remain distinctive, but it derives color,
spacing, type, controls, focus, and state presentation from the shared system.

### 6.3 Configure with confidence

Used by Settings, Apps, Privacy configuration, enrollment, rollout creation,
and administrative forms.

Forms use a readable column no wider than 720px unless a side-by-side review is
essential. Every field has a persistent label. Help text describes effect or
constraints. Related fields are grouped under a section title and short
description. Save and cancel actions appear together after the affected
content. Destructive actions use a separate region and explicit confirmation.

### 6.4 Route mapping

| Surface | Primary pattern | Notes |
|---|---|---|
| `/cockpit` | Monitor and compare | Decision summary, signals, grouped operational detail |
| `/operator` | Monitor + configure | Monitoring first; provisioning/edit forms use form sections |
| `/fleet` | Monitor + configure | Overview table, rollout form/history, enrollment keys |
| `/kpis` | Monitor and compare | Clear metric hierarchy and comparison groups |
| `/privacy` | Monitor + configure | Current posture before configuration controls |
| `/chat` | Focused workspace | Conversation rail, thread, composer |
| `/drive` | Focused workspace | Folder rail, toolbar, file list, detail/dialog surfaces |
| `/documents` | Focused workspace | Library and document review |
| `/ai-employees` | Focused workspace | Team context, compact tabs, task-specific canvas |
| `/spaces` | Configure with confidence | Connected apps and space controls |
| `/settings` | Configure with confidence | Signed-in account layout inside the shell |
| `/login` | Authentication | Dedicated centered layout using shared tokens |

## 7. Shared UI components

The existing feature components keep their domain state and API calls. Shared
presentation is consolidated behind typed primitives rather than duplicated
class combinations.

The shared layer includes:

- `ConsoleShell` for responsive navigation, context, identity, and workspace
  selection;
- `PageHeader` for domain, title, description, and actions;
- `Tabs` for section navigation and optional counts;
- `StatusSummary` for condition, explanation, next action, timestamp, and
  semantic tone;
- `MetricStrip` for the limited cases where several counts are useful;
- `Panel` or `SectionSurface` for one functional content boundary;
- `StatusBadge` for labeled semantic states;
- `DataTable` conventions for headers, grouped cells, actions, and disclosure;
- `FormSection`, field help, and field error conventions;
- `Notice` for section-level success, warning, and failure;
- `EmptyState`, `LoadingState`, and `ErrorState`; and
- dialog and mobile-drawer behavior.

Primitives expose semantic props such as `tone`, `title`, `description`, and
`actions`; feature components do not pass arbitrary colors. Static JSX is
defined outside render functions where practical, and expensive or optional
feature panels may retain their current dynamic imports.

## 8. Fleet reference design

Fleet is the reference screen because it currently demonstrates the hierarchy
and density problems most clearly.

The page title becomes **Deployments** under a Fleet domain label. The
description is “Monitor health, releases, and enrollment from one place.” The
top-level tabs remain Overview, Rollouts, and Enrollment keys.

The Overview begins with one state summary:

- “All deployments are healthy” when no deployment needs attention;
- a specific attention statement when alerts or missing signals exist; and
- the last refresh time as supporting metadata.

The table groups the current ten-plus fields into:

- disclosure;
- Health;
- Deployment: customer name, deployment ID, gate and ring context;
- Release: reported/current version and active duration;
- Activity: last report, receipt, and added date;
- Usage: users and storage; and
- Alerts.

The primary row stays 52–56px high. Expanding it reveals exact timestamps,
registry mismatch detail, host/storage detail, and other low-frequency metadata.
No data is removed; it is prioritized.

Rollouts use a narrow form section followed by history. Status copy explains
the state and the operator action. Pause, resume, and stop remain row actions
with a clear destructive distinction. Enrollment keys use the same form and
table conventions and preserve one-time token warnings.

## 9. Responsive behavior

### 9.1 Tablet, 768–1199px

- The sidebar becomes a 72px icon rail with accessible labels available to
  screen readers and on focus/hover.
- Page gutters reduce to 24px.
- Two-column grids collapse when either column would become unreadable.
- Operational tables keep only priority columns in the summary row; remaining
  fields remain available through disclosure.
- Tab rows may scroll horizontally without hiding the active state.

### 9.2 Mobile, below 768px

- The sidebar becomes a menu button and modal navigation drawer.
- The top bar keeps the OneBrain identity, page context, and account control.
- Page gutters reduce to 16px.
- Actions wrap below page copy instead of shrinking labels.
- Comparison tables become labeled records using the same semantic order;
  users are not forced to decode an unlabeled horizontally scrolling row.
- Form controls use the full available width.
- Dialogs become near-full-screen sheets when required.

Responsive presentation must not duplicate data-fetching or business logic.
The same semantic content is rearranged with CSS and small presentation
components.

## 10. Interaction and accessibility

- All interactive elements have a visible three-pixel focus ring with adequate
  offset.
- Keyboard order follows visual order. Drawers and dialogs trap focus and
  restore it on close.
- Hover never reveals the only copy of information or action.
- Color is always paired with a word, icon, or shape.
- Status announcements use appropriate `status` or `alert` semantics without
  making routine refreshes disruptive.
- Tables retain real headers on desktop; mobile records include visible or
  accessible labels.
- Touch targets are at least 40px on mobile, even when desktop controls are
  visually compact.
- Transitions are limited to 120–160ms for hover, selection, disclosure, and
  drawer state. `prefers-reduced-motion` disables nonessential motion.
- Long customer names, versions, emails, and error messages wrap or truncate
  with an accessible full value.

## 11. Loading, empty, error, and success states

Loading keeps the final page structure stable. Skeletons match the expected
content shape and do not replace the whole screen with a spinner.

Errors appear at the smallest boundary that can recover:

- field validation beside the field;
- section fetch failures inside that section with Retry; and
- page-level authorization or capability failures in the page content.

Empty states explain what is absent, why it matters, and the available next
action. They do not use decorative filler illustrations.

Action vocabulary stays consistent. For example, a button labeled “Save
changes” produces “Changes saved,” and a “Pause rollout” action produces
“Rollout paused.” Destructive actions state their target.

## 12. Data flow and performance boundaries

The redesign changes presentation, not API contracts. Existing feature panels
continue to own their requests and domain state. Independent requests continue
to execute in parallel. No new backend endpoints are required for layout or
progressive disclosure.

Implementation must preserve current performance practices:

- dynamically load genuinely heavy optional panels;
- avoid new barrel imports for large feature modules;
- derive display state during render instead of introducing synchronization
  effects;
- keep static configuration and JSX outside components where practical;
- avoid subscribing components to state used only inside event handlers; and
- use CSS for responsive layout instead of client-side viewport state.

The current `globals.css` is large and contains shared and feature-specific
rules. The redesign should establish the token system and clearly separated
shared shell/primitives without adding a late override pile. Feature-specific
CSS may remain co-located, as Drive already demonstrates, and obsolete global
rules must be removed as each surface migrates. A complete CSS-module migration
is not required for this redesign.

## 13. Implementation sequence

1. Establish tokens, reset, typography, focus, controls, badges, notices, and
   base responsive rules.
2. Redesign `ConsoleShell`, responsive navigation, top bar, and signed-in
   Settings integration.
3. Upgrade shared admin primitives and state components.
4. Make Fleet the reference monitor/configure implementation, including grouped
   responsive deployment rows.
5. Apply the monitor pattern to Status, Control, KPIs, and Privacy.
6. Apply the focused-workspace system to Ask, Drive, Documents, and AI
   Employees without erasing their task-specific layouts.
7. Apply the configure pattern to Apps, Settings, rollout, enrollment, and
   administrative forms.
8. Align authentication and exceptional state screens with the same tokens.
9. Remove obsolete CSS and perform the full responsive/accessibility pass.

Each step must leave the application buildable and preserve feature behavior.

## 14. Verification

Automated verification includes:

- `npm run lint`;
- `npm run typecheck`;
- `npm test`;
- `npm run build`; and
- existing console navigation and surface contracts.

Additional tests cover:

- capability-based navigation remains unchanged;
- responsive drawer semantics and active state;
- tabs expose selected state;
- status and notice roles;
- grouped Fleet data preserves every existing field;
- disclosure is keyboard operable;
- signed-in settings stay in the console shell; and
- loading, error, empty, and success copy remains actionable.

Visual verification uses representative screens at 1440px, 1024px, and 390px:

- Fleet;
- Status;
- Ask;
- Drive;
- AI Employees; and
- Settings.

Manual checks include keyboard navigation, focus restoration, mobile drawer,
overflow, long labels, empty collections, failed requests, loading stability,
reduced motion, and browser zoom at 200%.

## 15. Non-goals

- changing backend behavior or API schemas;
- renaming product capabilities or changing authorization;
- replacing the OneBrain identity or creating a new logo;
- adding charts where existing data does not require visualization;
- hiding operational data permanently to achieve minimalism;
- rewriting feature state management; or
- introducing a third-party component framework.

## 16. Completion criteria

The redesign is complete when:

- all routes use the shared token, typography, spacing, control, focus, and
  state system;
- every signed-in route uses the responsive console shell unless it is a
  deliberate full-screen task surface within that shell;
- page title, state, and next action are immediately understandable;
- Fleet and other operational tables prioritize and group information without
  losing data;
- desktop, tablet, and mobile layouts are intentionally designed and verified;
- loading, empty, error, success, and destructive states are consistent;
- the existing tests plus the new shell/primitives coverage pass;
- lint, typecheck, production build, and visual verification pass; and
- no obsolete competing style system remains on a migrated screen.
