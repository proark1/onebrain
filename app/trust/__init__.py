"""Trust primitives (Hetzner P0): offline release signing, the registry
allowlist, and the two-key signed desired-state envelope. The runtime only
ever VERIFIES — release/floor private-key operations exist solely in the
offline CLI (scripts/sign_release.py)."""
