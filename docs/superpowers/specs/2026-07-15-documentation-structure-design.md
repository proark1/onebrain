# Documentation Structure Design

**Status:** approved design
**Date:** 2026-07-15

## Purpose

Make OneBrain's documentation easy to operate from without losing the record of
earlier design work. OneBrain is a general, GDPR-conscious platform for
organizations; it is not an NFT Gym-specific product. Production architecture
is Hetzner-only.

## Problem

Current operational documents are intermingled with dated design and
implementation records. Several current entry points still describe Railway,
NFT Gym, or an older direct-Hetzner provisioning model that predates the
Mission Control and broker separation.

Readers need to be able to distinguish current instructions from historical
context immediately.

## Approved structure

```text
docs/
  README.md                         # current-documentation map
  archive/
    README.md                       # archive scope and status
    plans/                          # dated implementation plans, unchanged
    specs/                          # dated design specifications, unchanged
  ...current operational and contract documents...
```

The existing dated records in `docs/superpowers/specs/` and
`docs/superpowers/plans/` move to `docs/archive/specs/` and
`docs/archive/plans/` respectively. Their filenames and contents remain
unchanged. The archive is not an operational source of truth and may contain
retired Railway or product-specific assumptions.

## Current documentation

The documentation map will group maintained documents by purpose:

- Product and platform overview: general OneBrain scope and dedicated customer
  instances.
- Architecture and operations: Mission Control, the isolated full-stack dev
  gate, the Hetzner broker, customer isolation, and release promotion.
- Technical contracts: data ownership, intake, migrations, service client,
  and deletion/tombstone behavior.
- Historical archive: immutable dated specs and plans.

The root README, deployment guide, Mission Control runbook, release-promotion
guide, web README, and configuration example will be refreshed so that their
active guidance reflects the Hetzner-only model. The current architecture
documentation will make these boundaries explicit:

- Mission Control is the private global super-admin and deployment control
  plane; it does not handle customer content.
- The dev gate is an isolated, dummy-data, full customer suite used to validate
  updates before explicit customer rollout.
- Each customer has an isolated deployment and no fleet/control-plane access.
- The private broker holds the Hetzner API token and performs only bounded
  infrastructure actions requested by Mission Control.

## Preservation and link handling

- Historical documents will not be rewritten to remove obsolete statements.
- A short archive README and the documentation map will state their historical
  status instead.
- Links from current documents will point only to current documents unless a
  historical decision record is intentionally referenced.
- Links inside archived records may remain as recorded history; broken internal
  links caused by the move will be updated mechanically without changing the
  substance of the record.

## Non-goals

- Removing Railway code, workflows, or configuration from the repository.
- Changing deployment behavior or infrastructure credentials.
- Rewriting technical contracts that are still valid.
- Altering the contents or conclusions of the dated specs and plans.

## Verification

- Every dated plan and spec is present once in `docs/archive/` with its original
  name and content.
- The documentation map separates current docs, technical contracts, and the
  historical archive.
- Active documentation contains no prescriptive Railway or NFT Gym branding.
- Markdown links in maintained documentation resolve to their new targets.
- Repository search confirms the active docs and `.env.example` do not present
  Railway as the production/deployment path or NFT Gym as the product identity.
