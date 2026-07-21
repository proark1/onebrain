# Self-service password change design

## Goal

Give every authenticated OneBrain user a clear, self-service way to change their
own password. A user flagged as requiring a password rotation must be directed
to this flow before they can use protected console features such as chat.

## Scope

- Add a signed-in **Change password** destination at `/settings/password`.
- Add an account-menu link to that destination.
- Add fields for the current password, new password, and new-password
  confirmation.
- Reuse `POST /api/auth/change-password`; it remains the only mutation API.
- Surface `must_change_password` through the session response and frontend
  session type.
- Redirect a password-change-required user to the password page before rendering
  a protected console page.
- After a successful change, sign the user out and return them to login with a
  success notice. This matches the backend's deliberate revocation of all active
  sessions after a password change.

## Non-goals

- Administrators cannot set, reset, or view another user's password.
- This does not add password recovery, email reset links, or password policy
  changes beyond the existing backend minimum of 12 characters.

## Components and data flow

1. `GET /api/session/me` includes `must_change_password` from the authenticated
   principal.
2. The frontend maps that value into `SessionInfo`.
3. Protected console routes check the value and redirect to
   `/settings/password` when it is true. The password route itself remains
   available so the user cannot be trapped in a redirect loop.
4. The shared console account menu exposes **Change password** for every
   signed-in user.
5. The password form validates that confirmation matches and that the proposed
   password is at least 12 characters before sending the existing endpoint.
6. On success, the client calls logout to clear the browser cookie, then routes
   to `/login?passwordChanged=1`; the login screen explains that the user can
   sign in with the new password.

## Failure handling

- Incorrect current passwords and backend validation errors appear inline using
  the API's message.
- Network failures retain the entered form state and show a retryable error.
- The form disables submission while its request is in flight.
- The password values are never stored in React state, URLs, logs, or session
  storage; the form reads them only at submit time.

## Verification

- Backend session tests verify `must_change_password` is reported for the
  authenticated principal.
- Frontend tests verify required-change redirects and successful form handling.
- Existing auth tests continue to establish that protected endpoints reject a
  password-change-required session and the change endpoint clears the flag.
