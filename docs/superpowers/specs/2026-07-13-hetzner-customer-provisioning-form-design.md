# Hetzner customer provisioning form design

## Purpose

Update Mission Control's customer setup form so an operator can provision a real Hetzner customer deployment without manually entering release or infrastructure metadata. The form must offer only releases that can boot the selected bundle, clearly show the fixed provider and region, and collect the owner email required to create the first administrator account.

## Current problems

- Initial version is a free-text field initialized to `0.1.0`, even though release manifests are already registered in the control plane.
- Release creation dates are stored in `control_release_manifests.created_at` but omitted from the operator API response.
- The form offers Railway and other deployment types even though new deployments use Hetzner only.
- Region is blank and editable even though new deployments are pinned to Hetzner Nuremberg (`nbg1`).
- Hetzner provisioning requires `owner_email`, but the form and frontend request type do not provide it.
- External dispatch is optional and disabled by default, allowing an operator to create metadata without deploying a server.
- The dry-run checkbox is misleading on the Hetzner path because the Hetzner provisioner does not branch on `dry_run` and creates infrastructure regardless of its value.

## Decisions

### Release source and eligibility

The existing control-plane release manifest is the sole source of version choices. The operator release response will expose the manifest's `created_at` value and image map. No database migration is required.

A release is deployable for a bundle only when:

1. its status is `active`; and
2. its digest-pinned image map contains an entry for every module in the selected bundle.

The form will not offer draft, deprecated, image-less, or partially covered releases. This reflects the Hetzner renderer's existing fail-closed requirement that every enabled module have a digest-pinned image.

### Version selection

Replace the free-text initial-version field with a select control. Each option displays the version and release date in an unambiguous operator-facing format, for example `2026.7.2 - 12 Jul 2026`.

Eligible releases are ordered newest first by `created_at`, with version as a deterministic fallback when dates are equal or absent on legacy data. The newest eligible release is selected by default. When the bundle changes, retain the selected release if it remains eligible; otherwise select the newest eligible release.

If a bundle has no eligible release, show `No deployable release`, explain that an active release with images for all bundle modules is required, and disable the provisioning action.

### Fixed infrastructure

Keep deployment type and region visible so the operator can verify the target, but render both as read-only values rather than one-option dropdowns:

- Deployment type: display `Dedicated Hetzner server`; submit `dedicated_server`, which is an existing accepted control-plane value.
- Region: display `Nuremberg (nbg1)`; submit `nbg1`, matching the current Hetzner location identifier.

The provider executor remains selected by the server-side `provisioner_backend=hetzner` configuration. The form does not introduce a second provider switch.

### Required owner email

Add a required `Owner email` field. Include the normalized value as `owner_email` in the provisioning request so the backend can create the initial administrator and one-time password required by the Hetzner secret bundle.

Use a browser-native email input plus trimmed non-empty validation. Customer name, owner email, bundle, and eligible initial version must all be present before the form can submit.

### Dispatch behavior

Customer creation from this form always represents a real Hetzner deployment:

- submit `external_provisioning: true`;
- submit `dry_run: false`;
- submit the existing callback URL used to report provisioning progress;
- remove the `Dispatch external workflow` and `Dry run` checkboxes.

The release ring remains selectable because it controls update policy independently of infrastructure provider and region.

## Component and data flow changes

### Operator API

Add `created_at` to `ReleaseOut` and populate it from `ReleaseManifest.created_at`. Continue returning the existing release image map. This change is additive and backward compatible.

### Frontend types and client

- Add `created_at` and `images` to `OperatorRelease`.
- Add `owner_email` to `ProvisionCustomerInput` and serialize it in `provisionCustomer`.
- Preserve the current callback URL construction and send it for every customer provision from the operator form.

### Operator form

- Derive the selected bundle from the loaded bundle list.
- Derive eligible releases from release status and complete image coverage.
- Keep version state synchronized when releases or the selected bundle change.
- Replace deployment type and region state with fixed constants.
- Add owner-email state and clear it after successful provisioning.
- Keep the form open and existing values available when provisioning fails so the operator can correct the cause.

## Error handling

- Loading failures continue to use the existing operator error notice.
- No eligible release is handled before submission with an inline explanation and disabled action.
- Backend failures, including missing Hetzner configuration or dispatch failure, remain visible through the existing error notice.
- The form closes and clears customer-specific fields only after the provisioning request succeeds.

## Verification

Backend tests will verify that operator release responses include `created_at` from stored manifests.

Frontend verification will cover, through focused pure helpers or component-level assertions where supported by the existing test setup:

- active-only release filtering;
- complete bundle image coverage;
- newest-first option order and date labels;
- selection fallback after bundle changes;
- disabled submission with no eligible release;
- required owner email;
- submitted `dedicated_server` and `nbg1` values;
- automatic external dispatch with `dry_run: false`.

Run the relevant Python test suite, frontend lint, TypeScript type-check, and production build. Shipping must stop if checks fail, secrets are detected, unrelated local changes appear, or the branch cannot merge cleanly.

## Out of scope

- Adding or changing release manifests.
- Creating a dynamic provider/region configuration API.
- Implementing a true Hetzner dry-run mode.
- Changing release-ring behavior.
- Changing the server-side Hetzner provider configuration.
