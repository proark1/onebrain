"""Offline release-signing CLI — the ONLY place the release/floor private key
is ever used (ground rule: no RELEASE signing key on Mission Control, ever).

Subcommands:
  keygen      mint an Ed25519 keypair (base64 raw 32-byte keys)
  sign        sign a release-manifest JSON (a POST /api/operator/releases body);
              prints the STRIPPED manifest with `signature` filled in, ready to
              POST. Fields are normalized exactly the way the operator endpoint
              persists them (A6) so the stored row re-verifies.
  bump-floor  sign an onebrain-floor.v1 floor-bump statement (B3) — the actual
              kill mechanism for a yanked-but-still-signed release: run it after
              yanking to raise fleet floors past the yanked version.
  classify    classify a release's NEW migration files as code_only vs
              restore_required (the promotion-time rollback_kind linter, WP3).
              Pass only the delta file set — never the whole history. Exit code
              0 = code_only, 3 = restore_required (CI branches on it).

Run offline. The private key must never reach Mission Control or any deployed
environment variable.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.controlplane.migration_lint import ROLLBACK_RESTORE_REQUIRED, classify_release  # noqa: E402
from app.trust.envelope import FloorBump, sign_floor_bump  # noqa: E402
from app.trust.release import release_signature_fields_from_body, sign_release  # noqa: E402
from app.trust.signing import generate_keypair  # noqa: E402


def _read_private_key(path: str) -> str:
    return Path(path).read_text(encoding="utf-8").strip()


def _cmd_keygen(_args: argparse.Namespace) -> int:
    private_key_b64, public_key_b64 = generate_keypair()
    print(json.dumps({"private_key_b64": private_key_b64, "public_key_b64": public_key_b64}, indent=2))
    print(
        "WARNING: keep the private key OFFLINE. It must never reach Mission Control "
        "or any deployed environment variable — only the public key is configured "
        "(ONEBRAIN_RELEASE_VERIFY_PUBLIC_KEY).",
        file=sys.stderr,
    )
    return 0


def _cmd_sign(args: argparse.Namespace) -> int:
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        print("manifest must be a JSON object shaped like the release-create body", file=sys.stderr)
        return 2
    fields = release_signature_fields_from_body(manifest)
    signature = sign_release(fields, _read_private_key(args.private_key_file))
    signed = dict(manifest)
    signed.update(fields)  # the STRIPPED values — sign what will be stored (A6)
    signed["signature"] = signature
    if args.key_id:
        signed["signing_key_id"] = args.key_id
    print(json.dumps(signed, indent=2, sort_keys=True))
    return 0


def _cmd_bump_floor(args: argparse.Namespace) -> int:
    bump = FloorBump(
        deployment_scope=args.deployment or "*",
        floor_version=args.floor_version,
        issued_at=datetime.now(timezone.utc).isoformat(),
    )
    signed = sign_floor_bump(bump, _read_private_key(args.private_key_file))
    print(json.dumps(signed.model_dump(), indent=2, sort_keys=True))
    return 0


def _cmd_classify(args: argparse.Namespace) -> int:
    sql_files: list[tuple[str, str]] = []
    if args.sql_dir:
        for path in sorted(Path(args.sql_dir).glob("*.sql")):
            sql_files.append((path.name, path.read_text(encoding="utf-8")))
    for raw in args.sql_file or []:
        path = Path(raw)
        sql_files.append((path.name, path.read_text(encoding="utf-8")))
    alembic_sources = [
        (Path(raw).name, Path(raw).read_text(encoding="utf-8"))
        for raw in args.alembic_file or []
    ]
    result = classify_release(alembic_sources=alembic_sources, sql_files=sql_files)
    print(json.dumps(
        {"rollback_kind": result.rollback_kind, "findings": [asdict(f) for f in result.findings]},
        indent=2, sort_keys=True,
    ))
    return 3 if result.rollback_kind == ROLLBACK_RESTORE_REQUIRED else 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sign_release", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    keygen = subparsers.add_parser("keygen", help="mint an Ed25519 keypair")
    keygen.set_defaults(func=_cmd_keygen)

    sign = subparsers.add_parser("sign", help="sign a release-manifest JSON file")
    sign.add_argument("--manifest", required=True, help="path to the release JSON (POST body shape)")
    sign.add_argument("--private-key-file", required=True, help="file holding the base64 private key")
    sign.add_argument("--key-id", default="", help="optional signing_key_id rotation label")
    sign.set_defaults(func=_cmd_sign)

    bump = subparsers.add_parser("bump-floor", help="sign an onebrain-floor.v1 floor bump (B3)")
    bump.add_argument("--floor-version", required=True, help="new minimum version, e.g. 2026.07.3")
    bump.add_argument("--deployment", default="", help="deployment id scope (default: '*' = fleet-wide)")
    bump.add_argument("--private-key-file", required=True, help="file holding the base64 private key")
    bump.set_defaults(func=_cmd_bump_floor)

    classify = subparsers.add_parser(
        "classify", help="classify NEW migration files as code_only vs restore_required (exit 0/3)")
    classify.add_argument("--sql-dir", default="", help="directory of raw .sql migration files")
    classify.add_argument("--sql-file", action="append", default=[], help="one raw .sql migration file (repeatable)")
    classify.add_argument("--alembic-file", action="append", default=[],
                          help="one alembic migration .py file (repeatable)")
    classify.set_defaults(func=_cmd_classify)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
