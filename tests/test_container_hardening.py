"""Static guardrails for the non-root production image contract."""

from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]


def test_python_images_have_a_non_root_data_and_temp_contract():
    for name in ("Dockerfile", "Dockerfile.worker"):
        dockerfile = (_ROOT / name).read_text(encoding="utf-8")
        assert "USER onebrain" in dockerfile
        assert "VOLUME" not in dockerfile
        assert "PYTHONDONTWRITEBYTECODE=1" in dockerfile
        assert "TMPDIR=/tmp/onebrain" in dockerfile
        assert "install -d --owner=onebrain --group=onebrain --mode=0750 /data" in dockerfile


def test_web_image_keeps_only_explicit_runtime_cache_writable():
    dockerfile = (_ROOT / "onebrain-web" / "Dockerfile").read_text(encoding="utf-8")

    assert "FROM node:22-slim@sha256:" in dockerfile
    assert "COPY --from=build /app/.next ./.next" in dockerfile
    assert "USER onebrain" in dockerfile
    assert "TMPDIR=/tmp/onebrain" in dockerfile
    assert "XDG_CACHE_HOME=/tmp/onebrain/cache" in dockerfile
    assert "chown --recursive onebrain:onebrain /app/.next/cache" in dockerfile
