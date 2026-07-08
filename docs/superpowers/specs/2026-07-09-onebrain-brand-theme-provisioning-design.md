# OneBrain Brand Theme Provisioning

## Goal

OneBrain should keep the customer/project brand theme that is used when a new
OneBrain rollout provisions the assistant and AI communication tools. A rollout
can start with customer-provided colors, and every tool should inherit those
colors unless that tool has its own override.

## Architecture

The platform store owns brand themes because it already owns accounts, app
installations, access checks, and audit events. A brand theme is account-scoped
with an optional `app_id`:

- `app_id = ""` is the account/project default.
- `app_id = "assistant"` is the assistant override.
- `app_id = "communication"` is the AI communication override.

Theme resolution is deterministic:

1. return the active app-level theme when it exists;
2. otherwise return the active account-level theme;
3. otherwise return the built-in OneBrain default, currently based on
   `assad-dar.de`.

## Provisioning

`POST /api/provisioning/customers` accepts an optional `brand_theme`. When it is
provided, the provisioner stores it as the account default. When it is omitted,
the Assad Dar default is stored. The provisioning result includes the resolved
account theme and app themes so downstream deployment automation can pass the
same token set to the assistant and communication services.

## Tool Consumption

Human admins can read and update themes from platform APIs. Service callers can
read their resolved theme with their integration key so assistant and
communication deployments do not need a human session to bootstrap their UI
colors.

## Validation

Colors are stored as normalized `#rrggbb` values. The API rejects invalid colors
before writing platform records, so downstream tools receive complete and safe
CSS tokens.
