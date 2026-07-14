# Documentation Structure Implementation Plan

> This is an implementation record. It is stored in the archive so completed
> plans are not confused with current operational guidance.

## Scope

Implement the approved documentation structure in
`docs/superpowers/specs/2026-07-15-documentation-structure-design.md` without
changing historical spec or plan content.

## 1. Establish the documentation map

- Add `docs/README.md` as the maintained entry point.
- Group links into platform overview, Hetzner architecture and operations,
  technical contracts, and historical archive.
- State that OneBrain is a general platform and that Hetzner is the production
  deployment path.
- Add `docs/archive/README.md` explaining that archive contents are historical
  records rather than operational instructions.

## 2. Preserve historical records

- Move every dated file from `docs/superpowers/specs/` to `docs/archive/specs/`.
- Move every dated file from `docs/superpowers/plans/` to `docs/archive/plans/`.
- Preserve filenames and file contents exactly. The present plan is already in
  its destination and is excluded from the source move.
- Remove the now-empty `docs/superpowers/` directories.
- Update direct links to moved documents where a current document intentionally
  references a historical record.

## 3. Refresh active platform guidance

- Rewrite the root README as a concise general OneBrain overview, with the
  active Hetzner topology and local-development entry points.
- Replace Railway-centric deployment guidance with a Hetzner deployment and
  operational-safety guide.
- Update the Mission Control runbook to describe its super-admin-only role,
  its metadata boundary, and its broker dependency; it must not imply that MC
  directly holds a Hetzner API token.
- Update release promotion for the isolated `full_stack` dev gate and explicit
  customer rollout approval.
- Update the web README so it no longer prescribes Railway deployment.
- Rewrite oversized or stale top-level architecture documents in place as
  concise current references, retaining valid technical constraints while
  removing retired Railway/NFT Gym assumptions.
- Update `.env.example` comments and sample values that present Railway or NFT
  Gym as active deployment guidance; configuration keys for legacy code may
  remain but must be marked legacy/non-production.

## 4. Keep valid technical contracts focused

- Retain the data-layer, intake, migration, service-client, and
  deletion/tombstone contract documents.
- Normalize their links and map placement only; do not expand their scope or
  implement their proposed technical work.

## 5. Verify the documentation set

- Confirm every historical dated spec and plan occurs exactly once under
  `docs/archive/`.
- Search active documentation and `.env.example` for prescriptive Railway and
  NFT Gym references; only archived history may retain them.
- Check Markdown links in maintained docs and ensure source directories have
  no dangling document links.
- Run `git diff --check` and review the final file move set before commit.

## Delivery

Commit only documentation and `.env.example` changes, then push and fast-forward
the working branch into `main` if verification passes and the worktree remains
clean.
