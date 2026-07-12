"""Desired-state wrapper-key rotation helper (P5-02).

The desired-state wrapper key is MC's ONE ONLINE signing key (D-11) — it is NOT
the offline release key, and must never be confused with it. MC signs desired-state
with the single private key; boxes accept ANY public key in a delivered SET, so the
key can be rotated with no flag day.

Subcommands:
  keygen        mint a new Ed25519 wrapper keypair (base64 raw 32-byte keys).
  overlap-set   print the csv for ONEBRAIN_FLEET_DESIRED_STATE_PUBLIC_KEYS from an
                --old and --new public key (the rotation overlap set boxes accept).
  self-test     prove a minted pair signs+verifies and that the interlock's
                derive-public-from-private matches (no network).

Rotation sequence (no flag day; the box's ACCEPTED set is where overlap lives):
  1. keygen a new pair.
  2. Set ONEBRAIN_FLEET_DESIRED_STATE_PUBLIC_KEYS="<old_pub>,<new_pub>" (overlap-set)
     and POST /api/fleet/rotate-desired-state-key so every box re-fetches + accepts BOTH.
  3. Once every box's heartbeat echoes the new epoch (applied_secrets_epoch in
     /api/fleet/overview), swap ONEBRAIN_FLEET_DESIRED_STATE_PRIVATE_KEY to <new_priv>
     and restart MC. MC now signs with new; boxes already accept it.
  4. Set ...PUBLIC_KEYS="<new_pub>", bump epochs again; boxes drop old.
The G1-1 interlock refuses (409 + a startup assertion) any step that would leave MC
signing with a key absent from the served set, so a mis-order cannot brick the fleet.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey  # noqa: E402

from app.trust.signing import (  # noqa: E402
    generate_keypair,
    public_key_from_private,
    sign_payload,
    verify_payload,
)


def _validate_public_key(pub: str) -> None:
    """Raise if pub is not a base64 raw 32-byte Ed25519 public key."""
    Ed25519PublicKey.from_public_bytes(base64.b64decode(pub))


def _cmd_keygen(_args: argparse.Namespace) -> int:
    private_key_b64, public_key_b64 = generate_keypair()
    print(json.dumps({"private_key_b64": private_key_b64, "public_key_b64": public_key_b64}, indent=2))
    print(
        "WARNING: this is the ONLINE desired-state wrapper key. Set the PRIVATE key as "
        "ONEBRAIN_FLEET_DESIRED_STATE_PRIVATE_KEY on Mission Control ONLY. It is NOT the "
        "offline release key (ONEBRAIN_RELEASE_VERIFY_PUBLIC_KEY / scripts/sign_release.py) "
        "and must never be confused with it. Add the PUBLIC key to the served set "
        "(ONEBRAIN_FLEET_DESIRED_STATE_PUBLIC_KEYS) so boxes accept it.",
        file=sys.stderr,
    )
    return 0


def _cmd_overlap_set(args: argparse.Namespace) -> int:
    for label, pub in (("--old", args.old), ("--new", args.new)):
        try:
            _validate_public_key(pub)
        except Exception as exc:  # noqa: BLE001 — surface a clear CLI error
            print(f"{label} is not a base64 raw 32-byte Ed25519 public key: {exc}", file=sys.stderr)
            return 2
    # Preserve order (old first) and drop an accidental duplicate.
    ordered = [args.old] + ([args.new] if args.new != args.old else [])
    print(",".join(ordered))
    return 0


def _cmd_self_test(_args: argparse.Namespace) -> int:
    private_key_b64, public_key_b64 = generate_keypair()
    payload = b'{"contract":"desired-state.v1","self-test":true}'
    signature = sign_payload(payload, private_key_b64)
    sign_verify_ok = verify_payload(payload, signature, public_key_b64)
    # The interlock derives the served public key from the private key — prove parity.
    derived_matches = public_key_from_private(private_key_b64) == public_key_b64
    print(json.dumps({"sign_verify_ok": sign_verify_ok, "derived_public_matches": derived_matches}, indent=2))
    return 0 if (sign_verify_ok and derived_matches) else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rotate_desired_state_key", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    keygen = subparsers.add_parser("keygen", help="mint a new Ed25519 wrapper keypair")
    keygen.set_defaults(func=_cmd_keygen)

    overlap = subparsers.add_parser(
        "overlap-set", help="print the ONEBRAIN_FLEET_DESIRED_STATE_PUBLIC_KEYS csv from --old + --new")
    overlap.add_argument("--old", required=True, help="the current (old) wrapper public key, base64")
    overlap.add_argument("--new", required=True, help="the new wrapper public key, base64")
    overlap.set_defaults(func=_cmd_overlap_set)

    self_test = subparsers.add_parser("self-test", help="prove a minted pair signs+verifies (no network)")
    self_test.set_defaults(func=_cmd_self_test)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
