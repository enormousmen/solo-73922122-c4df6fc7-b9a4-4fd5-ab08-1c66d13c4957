"""Shared fixtures: deterministic OCI layouts built on the fly."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.oci import OciLayout  # noqa: E402
from app.store import StableStore  # noqa: E402
from scripts.build_layout import (  # noqa: E402
    LayoutBuilder,
    build_demo_layout,
    canonical_json,
    sha256_digest,
)


@pytest.fixture
def demo(tmp_path: Path) -> dict:
    layout_dir = tmp_path / "oci-layout"
    meta = build_demo_layout(layout_dir, tmp_path / "digests.json")
    return meta


@pytest.fixture
def settings(tmp_path: Path, demo: dict) -> Settings:
    return Settings.from_env(
        {
            "OCI_LAYOUT_DIR": str(tmp_path / "oci-layout"),
            "STATE_DIR": str(tmp_path / "state"),
            "OCI_ARCHITECTURES": "amd64,arm64",
            "LAYER_CHUNK_BYTES": "1024",
        }
    )


def make_client(settings: Settings):
    from fastapi.testclient import TestClient

    return TestClient(create_app(settings))


@pytest.fixture
def client(settings: Settings):
    with make_client(settings) as test_client:
        yield test_client


def release_body(demo: dict, version: str, arch: str, previous_version: str) -> dict:
    return {
        "platform": f"linux/{arch}",
        "manifestDigest": demo["versions"][version][arch]["digest"],
        "expectedPreviousDigest": demo["versions"][previous_version][arch]["digest"],
    }


def stable_by_arch(index: dict) -> dict[str, dict]:
    return {m["platform"]["architecture"]: m for m in index["manifests"]}


def write_blob(layout_dir: Path, data: bytes) -> tuple[str, int]:
    digest = sha256_digest(data)
    path = layout_dir / "blobs" / "sha256" / digest.split(":", 1)[1]
    path.write_bytes(data)
    return digest, len(data)


def read_blob(layout_dir: Path, digest: str) -> bytes:
    return (layout_dir / "blobs" / "sha256" / digest.split(":", 1)[1]).read_bytes()


def append_garbage_to_layer(layout_dir: Path, manifest_digest: str) -> None:
    manifest = json.loads(read_blob(layout_dir, manifest_digest))
    layer_digest = manifest["layers"][0]["digest"]
    path = layout_dir / "blobs" / "sha256" / layer_digest.split(":", 1)[1]
    data = path.read_bytes()
    path.write_bytes(data + b"tampered")


def rewrite_manifest_with_bad_layer_digest(layout_dir: Path, manifest_digest: str) -> str:
    manifest = json.loads(read_blob(layout_dir, manifest_digest))
    manifest["layers"][0]["digest"] = "sha256:" + "0" * 64
    data = canonical_json(manifest)
    digest, _ = write_blob(layout_dir, data)
    return digest


def rewrite_manifest_size(layout_dir: Path, manifest_digest: str, new_size: int) -> str:
    manifest = json.loads(read_blob(layout_dir, manifest_digest))
    manifest["config"]["size"] = new_size
    data = canonical_json(manifest)
    digest, _ = write_blob(layout_dir, data)
    return digest


def fresh_store(settings: Settings) -> StableStore:
    layout = OciLayout(settings.oci_layout)
    return StableStore(settings, layout)


__all__ = [
    "LayoutBuilder",
    "append_garbage_to_layer",
    "canonical_json",
    "demo",
    "fresh_store",
    "make_client",
    "read_blob",
    "release_body",
    "rewrite_manifest_size",
    "rewrite_manifest_with_bad_layer_digest",
    "sha256_digest",
    "stable_by_arch",
    "write_blob",
]
