#!/usr/bin/env python3
"""Bulk-import a folder of documents into onebrain.

Every file in the folder is uploaded with the SAME label. To apply different
labels, sort files into folders and run this once per folder, e.g.:

    python scripts/bulk_import.py ./public_docs --classification public     --category general
    python scripts/bulk_import.py ./hr_docs     --classification restricted --category hr

Target the deployed app with --url or the ONEBRAIN_URL environment variable:

    ONEBRAIN_URL=https://onebrain-production-0a16.up.railway.app \\
        python scripts/bulk_import.py ./docs --classification internal --category ops
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("This script needs `requests`:  pip install requests")

DEFAULT_EXTS = ".pdf,.docx,.xlsx,.xlsm,.pptx,.rtf,.txt,.md,.csv,.json,.html,.png,.jpg,.jpeg,.tiff,.bmp,.webp"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Bulk-import documents into onebrain.")
    p.add_argument("folder", type=Path, help="Folder of documents to upload")
    p.add_argument("--url", default=os.environ.get("ONEBRAIN_URL", "http://127.0.0.1:8000"),
                   help="onebrain base URL (or set ONEBRAIN_URL)")
    p.add_argument("--role", default="admin", help="Employee role to upload as (default: admin)")
    p.add_argument("--classification", default="internal",
                   choices=["public", "internal", "confidential", "restricted"])
    p.add_argument("--location", default="global")
    p.add_argument("--category", default="general")
    p.add_argument("--ext", default=DEFAULT_EXTS,
                   help=f"Comma-separated extensions to include (default: {DEFAULT_EXTS})")
    p.add_argument("--recursive", action="store_true", help="Include sub-folders")
    p.add_argument("--dry-run", action="store_true", help="List files without uploading")
    return p.parse_args()


def collect_files(folder: Path, exts: set[str], recursive: bool) -> list[Path]:
    walker = folder.rglob("*") if recursive else folder.glob("*")
    return sorted(f for f in walker if f.is_file() and f.suffix.lower() in exts)


def main() -> int:
    args = parse_args()
    if not args.folder.is_dir():
        sys.exit(f"Not a folder: {args.folder}")

    exts = {e if e.startswith(".") else f".{e}" for e in args.ext.lower().split(",") if e}
    files = collect_files(args.folder, exts, args.recursive)
    if not files:
        sys.exit(f"No matching files ({', '.join(sorted(exts))}) in {args.folder}")

    label = f"{args.classification} / {args.category} / {args.location}"
    print(f"Uploading {len(files)} file(s) to {args.url}")
    print(f"  role: {args.role}   label: {label}\n")

    if args.dry_run:
        for f in files:
            print(f"  would upload  {f.name}")
        print("\n(dry run — nothing uploaded)")
        return 0

    headers = {"X-Onebrain-Role": args.role, "X-Onebrain-Location": args.location}
    data = {"classification": args.classification, "location": args.location, "category": args.category}
    ok = failed = 0

    for f in files:
        try:
            with f.open("rb") as fh:
                resp = requests.post(f"{args.url}/api/upload", headers=headers, data=data,
                                     files={"file": (f.name, fh)}, timeout=180)
            if resp.status_code == 200:
                print(f"  ✓ {f.name}  ({resp.json().get('chunks', '?')} chunks)")
                ok += 1
            else:
                detail = resp.json().get("detail", resp.text[:120]) if resp.content else resp.reason
                print(f"  ✗ {f.name}  [{resp.status_code}] {detail}")
                failed += 1
        except Exception as exc:  # network error, timeout, etc.
            print(f"  ✗ {f.name}  {type(exc).__name__}: {exc}")
            failed += 1

    print(f"\nDone: {ok} uploaded, {failed} failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
