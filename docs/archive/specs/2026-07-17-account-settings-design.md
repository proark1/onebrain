# Account settings design

## Goal

Provide an obvious, signed-in account settings area that gives every user access
to password management and logout in the Next.js console.

## Scope

- Add `/settings` as the account-settings landing page.
- Link the top-right account identity to `/settings`.
- Present a **Change password** action that leads to `/settings/password`.
- Present a visible **Log out** action on the settings page.
- Keep `/settings/password` as the dedicated form route.
- Continue to route users whose password must be changed directly to the
  password form before they can use the protected console.

## Components and data flow

1. The console account identity links to `/settings`.
2. The settings page requires an authenticated session and renders account
   actions for any signed-in role.
3. Choosing **Change password** opens the existing self-service form.
4. Choosing **Log out** calls `POST /api/auth/logout`, which revokes the server
   session and clears the browser cookie, then routes to `/login`.
5. The existing `must_change_password` guard continues to redirect protected
   console pages to `/settings/password`; the settings/password routes remain
   accessible to avoid redirect loops.

## Failure handling

- A failed logout displays an inline retryable error and does not navigate away.
- The logout button disables while the request is running, preventing duplicate
  requests.
- The password form keeps its existing validation and logout-after-success
  behavior.

## Verification

- Existing auth/session tests verify the backend logout and password-change
  behavior.
- Frontend type checking and linting verify the Settings UI integration.
- Manual verification: from Settings, logout returns to login; signing in again
  restores console access; a password-change-required session reaches the
  password form.
