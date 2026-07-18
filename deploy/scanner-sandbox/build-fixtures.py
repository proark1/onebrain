"""Build deterministic, content-free ClamAV image-gate fixtures."""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return output.getvalue()


def main() -> int:
    root = Path(sys.argv[1])
    root.mkdir(parents=True, exist_ok=True)
    (root / "member-count.zip").write_bytes(
        _zip_bytes({f"member-{index}.txt": b"bounded\n" for index in range(4)})
    )
    (root / "file-size.bin").write_bytes(b"F" * 4096)
    (root / "scan-size.zip").write_bytes(
        _zip_bytes({f"scan-{index}.txt": bytes([65 + index]) * 2048 for index in range(3)})
    )
    nested = _zip_bytes({"leaf.txt": b"recursion boundary\n"})
    for depth in range(6):
        nested = _zip_bytes({f"level-{depth}.zip": nested})
    (root / "recursion.zip").write_bytes(nested)
    (root / "scan-time.zip").write_bytes(
        _zip_bytes({"expensive.txt": b"OneBrain scan-time boundary.\n" * 2_000_000})
    )
    for path in root.iterdir():
        if path.is_file():
            path.chmod(0o444)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
