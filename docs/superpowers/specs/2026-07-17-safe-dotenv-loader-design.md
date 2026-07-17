# Safe Runtime Dotenv Loading

## Problem

The provisioning bundle intentionally writes raw `KEY=value` entries to
`/opt/onebrain/.env` so Docker Compose receives the exact configured values.
Several privileged host scripts then source that file as shell code. A password
or key containing shell syntax can make an update fail or be interpreted by the
shell. On 17 July, Mission Control's updater stopped before fetching desired
state because the administrator password contained shell-significant characters.

## Goal

Host-side update, bootstrap, gate-agent, and provisioning-callback code must
read literal bundle values without evaluating them. Existing Compose dotenv
behavior and trusted `box.env` variable references must remain compatible.

## Chosen approach

Add one small POSIX-compatible loader, packaged on each Hetzner box as
`/opt/onebrain/onebrain_dotenv.sh`. It reads canonical `KEY=value` lines with
`IFS= read -r`, accepts only shell-valid environment variable names, and exports
each complete value literally with `export "$key=$value"`.

The loader skips blank and comment lines. A malformed non-comment entry causes a
safe, non-destructive hold before network, Docker, or Compose work begins. It
never prints the rejected value.

`box.env` remains shell-sourced after the literal loader has populated the
environment. It is a generated, trusted script that intentionally expands
references such as `${ONEBRAIN_FLEET_KEY}`. Nounset is temporarily relaxed while
loading it so a box with no exchanged secret bundle holds harmlessly instead of
aborting.

This is preferred over shell-escaping bundle values because Compose must retain
its established raw dotenv semantics. It is preferred over a new secret-file
format because that would require a wider provisioning and Compose migration.

## Consumers and data flow

1. `update.sh` loads `/opt/onebrain/.env` through the helper, then loads
   `box.env`, and only then fetches signed desired state.
2. `onebrain_bootstrap.sh` uses the helper when it reads an existing bundle for
   credential rotation.
3. `onebrain-gate-agent.sh` uses the helper before it invokes the updater and
   sends its authenticated heartbeat.
4. Cloud-init callback commands use the helper before they load `box.env`, so
   callback authentication receives literal bootstrap values.
5. Docker Compose continues to read `.env` directly and is unchanged.

## Error handling and safety

- Missing `.env` remains supported for an unbootstrapped box.
- Missing variables referenced by `box.env` produce an inert hold; they do not
  execute shell text or mutate local images, Compose files, or data.
- Literal values preserve spaces, hashes, equals signs, dollar signs, quotes, and
  command-substitution syntax as data.
- The helper is root-owned through the existing cloud-init asset path and is not
  read by application containers.
- No release-signing, desired-state verification, pull policy, or Compose image
  selection changes.

## Verification

Tests will run the real shell scripts with a literal value containing spaces,
hashes, equals signs, dollar signs, and a command-substitution marker. They will
prove that no marker executes and that trusted `box.env` references receive the
exact literal value. Additional coverage will verify bootstrap rotation, a missing
bundle's clean hold, malformed dotenv rejection without leakage, and generated
cloud-init packaging/callback behavior. Existing full Python and frontend checks
will run before shipping.
