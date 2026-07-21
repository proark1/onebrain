# Settings sidebar and development-gate rollout design

## Goal

Make Account Settings discoverable in the console and deliver the verified
password-change/logout capability to the isolated development-gate deployment.

## Scope

- Add **Settings** to the shared left sidebar for every signed-in user.
- Keep the top-right account-name link to Settings.
- Preserve the existing password-change-required redirect to
  `/settings/password`.
- Build and validate the application locally.
- Create a versioned release and roll it out only to the development gate using
  the repository's signed release and controlled rollout process.

## UI behavior

Settings appears below Privacy in the customer navigation. It opens `/settings`,
which offers Change password and Log out. Users whose account requires a
password change bypass ordinary console routes and are directed to the password
form until the change completes.

## Release behavior

A GitHub push alone does not update a customer-shaped environment. After local
checks pass, the release is published as immutable artifacts, recorded as a
release, and deliberately selected for the development gate. Health and the
Settings route are verified after rollout. No customer deployment is changed.

## Failure handling

- If build or application checks fail, no release is created.
- If gate health verification fails, preserve or restore the prior known-good
  release according to the rollout controls.
- If the release-control credentials or a required approval are unavailable,
  stop before changing the deployment and report the exact blocker.

## Verification

- Frontend typecheck, lint, and production build pass.
- Focused auth/session tests pass for password rotation and logout.
- The development-gate Settings route renders after rollout.
- Logging out from the gate returns the user to login; a password-change-
  required account reaches the password form rather than Status or Ask.
