# OneBrain Admin Console Redesign Design

Date: 2026-07-09

## Goal

Redesign the full OneBrain admin console in one coordinated pass so it feels like a modern operational control room: tighter, clearer, faster to scan, and more trustworthy. The redesign covers Chat, Documents, Spaces, Privacy, Operator, login, and shared shell/navigation.

The redesign must preserve the Assad Dar brand palette from `assad-dar.de` while adapting it for dense admin workflows instead of marketing-page presentation.

## Current Problems

The current UI works functionally, but it feels too spacious and demo-like for an admin console:

- The warm grid background and repeated bordered cards make every section compete for attention.
- Page headings are too large for operational surfaces.
- Admin screens expose too many forms and details at once.
- Global CSS mixes shell, page, form, chat, table, and status styles in one large file.
- Similar components are reimplemented inside panels instead of shared.
- Tables are represented as loose cards, which lowers scan speed for documents, customers, deployments, audit events, and review queues.
- Advanced or dangerous actions are always visible instead of being guided, staged, or confirmed.
- Mobile behavior collapses layout but does not prioritize workflows.

## Brand System

Use Assad Dar brand colors as the base tokens, not a generic blue SaaS palette.

Core colors:

- Graphite: `#16191e`
- Graphite hover: `#272c34`
- Slate: `#3e5573`
- Copper: `#a66e2f`
- Warm canvas: `#f4f2ee`
- Surface: `#ffffff`
- Soft surface: `#f1ece2`
- Text: `#101828`
- Muted: `#5f6671`
- Hairline: `#ddd6ca`
- Strong line: `#c6bfb1`
- Success: `#1f7a4d`
- Warning: `#b98a4e`
- Danger: `#b4453e`

Application treatment:

- Use graphite for the sidebar, high-emphasis text, and primary navigation.
- Use copper sparingly for active accents, focus rings, key calls to action, and highlighted state.
- Use slate for secondary actions, links, and informational status.
- Use warm canvas only as a subtle app background, not as a decorative grid.
- Use white and soft surface for dense work areas.
- Use semantic colors only for status and destructive actions.

The resulting console should still feel connected to Assad Dar, but more disciplined and operational than the public site.

## Typography

Use a restrained, admin-first type system:

- Primary UI font: existing system/Geist-compatible sans-serif stack.
- Data font: mono stack only for IDs, keys, hashes, run ids, deployment ids, and audit metadata.
- Page titles: 24-30px on desktop, never oversized hero typography.
- Panel headings: 16-18px.
- Table/list rows: 13-14px body text.
- Labels and metadata: 11-12px, uppercase only where it improves scanability.

No viewport-width font scaling. No negative letter spacing.

## Layout Architecture

Use a true console frame:

```text
+----------------------+----------------------------------------+
| Sidebar              | Top command bar                        |
| OneBrain             | Workspace / account / role / actions   |
| Chat                 +----------------------------------------+
| Documents            | Page title, tabs, filters, notices     |
| Spaces               +----------------------------------------+
| Privacy              | Main workflow area                     |
| Operator             | Dense lists, forms, detail panels      |
| User / role context  |                                        |
+----------------------+----------------------------------------+
```

Shell requirements:

- Sidebar stays fixed on desktop and compresses into a top navigation on mobile.
- Active navigation uses graphite fill with a copper left rail or underline.
- Workspace context moves into a top command bar so the sidebar is not overloaded.
- The top command bar shows selected workspace/account, role, location, and a small refresh/action area.
- Content max width is controlled per page, but page sections are not floating cards inside cards.

## Shared Components

Create shared UI primitives before redesigning individual screens:

- `AppShell`: sidebar, top command bar, responsive navigation.
- `PageHeader`: title, supporting metadata, primary action, optional tabs.
- `MetricStrip`: compact summary numbers with status-aware styling.
- `Panel`: simple section wrapper for forms or detail surfaces.
- `DataTable` / `DenseList`: scan-friendly rows with consistent metadata and actions.
- `StatusBadge`: success, warning, danger, neutral, running, draft, disabled.
- `Field`, `SelectField`, `TextAreaField`, `CheckboxGroup`: consistent labels, hints, errors.
- `Notice`: inline success/error/warning states.
- `EmptyState`: compact, actionable, not decorative.
- `Tabs`: screen-level and panel-level segmentation.
- `ActionBar`: row and page action grouping.
- `ConfirmDanger`: confirmation pattern for destructive actions.

Keep these components small and code-local to `onebrain-web/src/components` unless an existing pattern suggests a better split.

## Screen Designs

### Chat

Purpose: ask OneBrain questions from approved knowledge with visible source grounding.

Design changes:

- Keep chat as the default first-screen experience, but tighten the page.
- Conversation rail becomes compact and collapsible on smaller screens.
- Composer stays visually anchored at the bottom of the chat area.
- Assistant answers use clean message cards with source chips in a structured footer.
- Empty state becomes compact with 3 prompt buttons, not a large centered marketing block.
- Scope/workspace context appears in the top command bar and as a small chip near the composer.

### Documents

Purpose: manage knowledge ingestion, approval, and retrieval visibility.

Design changes:

- Convert document library from card rows into a dense table/list with columns: title, classification, category, location, chunks, PII, status.
- Add compact filter bar for classification, category, location, status, and search.
- Move upload into a right-side panel or drawer-style section.
- Pending approval becomes a dedicated queue with clear approve actions and risk flags.
- Use classification colors as subtle left rails or badges, not dominant row styling.
- Keep upload labels obvious because they decide retrieval permissions.

### Spaces

Purpose: manage accounts, spaces, app access, policy, and audit.

Design changes:

- Use master/detail layout:
  - Left: account list with search and create account action.
  - Right: selected account details.
- Detail area uses tabs:
  - Overview
  - Spaces
  - Apps
  - Policy
  - Audit
- Create forms should open inline in the relevant tab or as compact panels, not always occupy prime screen space.
- Access check appears in Policy with result state next to the controls.
- Audit becomes a dense timeline/table with actor, action, target, decision, purpose, and timestamp.

### Privacy

Purpose: safely export or erase scoped account data.

Design changes:

- Make this a guided two-column workflow:
  - Left: select account and optional space.
  - Right: action panel for export or erase.
- Export is a normal primary/secondary action.
- Erase is staged:
  - Scope preview.
  - Explicit confirmation input.
  - Reason field.
  - Danger button.
- Last result appears as a compact result panel with counts and audit id.
- Destructive actions use danger color only at the action point, not across the whole screen.

### Operator

Purpose: provision customers, track readiness, manage releases, and run rollouts.

Design changes:

- Split into cockpit tabs:
  - Customers
  - Provisioning
  - Releases
  - Rollouts
  - Credentials
- Top cockpit shows compact health metrics: customers, deployed, healthy, attention, active keys.
- Customers tab uses a dense table with readiness, backup, health, rollout, modules, keys, and row actions.
- Provisioning tab becomes a guided form with bundle preview and brand theme editor.
- Releases tab separates manifest list from creation form.
- Rollouts tab focuses on target version, plan, backup/health, rollout status, and terminal actions.
- Credentials tab only shows newly minted credentials when relevant, with copy actions and warning copy.

### Login

Purpose: clean, trustworthy access point.

Design changes:

- Use Assad Dar brand mark and compact centered login panel.
- Reduce marketing copy.
- Use clear error states.
- Keep focus and password manager compatibility.

## Interaction Rules

- Primary actions should be visually rare and obvious.
- Destructive actions require confirmation or a staged flow.
- Loading states should preserve layout and label the action in progress.
- Empty states should explain what to do next in one sentence.
- Errors should be placed near the affected workflow when possible.
- IDs and keys should use mono text with copy affordances where useful.
- Tables/lists should not resize or jump when status labels change.
- Mobile should prioritize one workflow per view, with navigation and details collapsible.

## Implementation Boundaries

Do not change backend API behavior as part of the UI redesign.

Allowed changes:

- Next.js components under `onebrain-web/src/components`.
- Next.js routes under `onebrain-web/src/app`.
- Global and component CSS under `onebrain-web/src/app/globals.css` or new CSS modules if useful.
- TypeScript-only UI helpers.

Avoid:

- Backend schema changes.
- API contract changes.
- New frontend frameworks.
- Marketing landing page patterns.
- Decorative gradients, orbs, or heavy illustration.
- Nested cards and page sections styled as floating cards.

## Verification

Run:

- `npm run lint`
- `npm run typecheck`
- `npm run build`

Visual QA:

- Desktop screenshot for Chat, Documents, Spaces, Privacy, Operator, Login.
- Mobile screenshot for Chat, Documents, Operator.
- Check no button text overflows.
- Check dense rows remain readable.
- Check status badges and destructive actions are distinguishable.
- Check keyboard focus is visible.
- Check brand colors are present but not overpowering.

## Rollout

Implement in one branch as a single coordinated UI redesign.

Because the console shares one global stylesheet today, complete the shared shell and token pass first, then migrate each screen to the shared primitives. This prevents a half-modern, half-old UI during development and keeps the final experience coherent.
