"""P5-02: the desired-state wrapper-key rotation CLI (scripts/rotate_desired_state_key.py).
Pure, no network — mints keys, builds the overlap-set csv, and self-tests the pair."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from app.trust.signing import generate_keypair, public_key_from_private


def _cli():
    path = Path(__file__).resolve().parents[1] / "scripts" / "rotate_desired_state_key.py"
    spec = importlib.util.spec_from_file_location("rotate_desired_state_key_cli", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_keygen_prints_pair_and_derive_matches(capsys):
    cli = _cli()
    assert cli.main(["keygen"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out) == {"private_key_b64", "public_key_b64"}
    # The printed public key is exactly the interlock's derived key.
    assert public_key_from_private(out["private_key_b64"]) == out["public_key_b64"]


def test_overlap_set_prints_ordered_csv(capsys):
    cli = _cli()
    _p1, old_pub = generate_keypair()
    _p2, new_pub = generate_keypair()
    assert cli.main(["overlap-set", "--old", old_pub, "--new", new_pub]) == 0
    assert capsys.readouterr().out.strip() == f"{old_pub},{new_pub}"


def test_overlap_set_dedupes_identical_keys(capsys):
    cli = _cli()
    _p, pub = generate_keypair()
    assert cli.main(["overlap-set", "--old", pub, "--new", pub]) == 0
    assert capsys.readouterr().out.strip() == pub  # not "pub,pub"


def test_overlap_set_rejects_malformed_public_key(capsys):
    cli = _cli()
    _p, good = generate_keypair()
    assert cli.main(["overlap-set", "--old", good, "--new", "not-a-key"]) == 2
    assert "not a base64 raw 32-byte" in capsys.readouterr().err


def test_self_test_passes(capsys):
    cli = _cli()
    assert cli.main(["self-test"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"sign_verify_ok": True, "derived_public_matches": True}


def test_requires_a_subcommand():
    cli = _cli()
    with pytest.raises(SystemExit):   # argparse: subcommand required
        cli.main([])
