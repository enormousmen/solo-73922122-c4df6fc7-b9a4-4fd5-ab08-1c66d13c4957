"""Unit-level checks of layout verification and configuration."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from app.config import ConfigError, Settings
from app.oci import (
    LayoutError,
    OciLayout,
    ReleaseValidationError,
    digest_bytes,
    verify_release,
)
from tests.conftest import (
    LayoutBuilder,
    append_garbage_to_layer,
    canonical_json,
    fresh_store,
    read_blob,
    sha256_digest,
    write_blob,
)


def test_verify_release_reports_chunked_hashing(settings, demo):
    layout = OciLayout(settings.oci_layout)
    verified = verify_release(
        layout, settings, "amd64", demo["versions"]["v2"]["amd64"]["digest"]
    )
    manifest = json.loads(
        read_blob(settings.oci_layout, demo["versions"]["v2"]["amd64"]["digest"])
    )
    expected_chunks = sum(
        math.ceil(layer["size"] / settings.layer_chunk_bytes)
        for layer in manifest["layers"]
    )
    assert verified.chunks == expected_chunks
    assert verified.layer_count == len(manifest["layers"])
    assert verified.layer_bytes == sum(layer["size"] for layer in manifest["layers"])
    assert verified.platform == "linux/amd64"
    assert verified.descriptor["platform"] == {"architecture": "amd64", "os": "linux"}
    assert verified.descriptor["digest"] == demo["versions"]["v2"]["amd64"]["digest"]
    assert verified.descriptor["size"] == len(
        read_blob(settings.oci_layout, demo["versions"]["v2"]["amd64"]["digest"])
    )


def test_arm64_descriptor_keeps_variant(settings, demo):
    layout = OciLayout(settings.oci_layout)
    verified = verify_release(
        layout, settings, "arm64", demo["versions"]["v2"]["arm64"]["digest"]
    )
    assert verified.descriptor["platform"]["variant"] == "v8"


def test_layer_truncation_is_rejected(settings, demo):
    manifest_digest = demo["versions"]["v2"]["amd64"]["digest"]
    manifest = json.loads(read_blob(settings.oci_layout, manifest_digest))
    layer_path = settings.oci_layout / "blobs" / "sha256" / manifest["layers"][0][
        "digest"
    ].split(":", 1)[1]
    original = layer_path.read_bytes()
    layer_path.write_bytes(original[: len(original) // 2])

    layout = OciLayout(settings.oci_layout)
    with pytest.raises(ReleaseValidationError, match="does not match blob size"):
        verify_release(layout, settings, "amd64", manifest_digest)


def test_layer_appended_bytes_are_rejected(settings, demo):
    manifest_digest = demo["versions"]["v2"]["amd64"]["digest"]
    append_garbage_to_layer(settings.oci_layout, manifest_digest)
    layout = OciLayout(settings.oci_layout)
    with pytest.raises(ReleaseValidationError, match="does not match blob size"):
        verify_release(layout, settings, "amd64", manifest_digest)


def test_manifest_json_tampering_is_rejected(settings, demo):
    """Same blob path, different bytes: the claimed digest no longer matches."""
    digest = demo["versions"]["v2"]["amd64"]["digest"]
    manifest = json.loads(read_blob(settings.oci_layout, digest))
    manifest["annotations"]["org.opencontainers.image.version"] = "tampered"
    path = settings.oci_layout / "blobs" / "sha256" / digest.split(":", 1)[1]
    path.write_bytes(canonical_json(manifest))

    layout = OciLayout(settings.oci_layout)
    with pytest.raises(ReleaseValidationError, match="does not match the submitted digest"):
        verify_release(layout, settings, "amd64", digest)


def test_manifest_not_schema2_rejected(settings, demo):
    manifest = {"schemaVersion": 1, "mediaType": "application/vnd.oci.image.manifest.v1+json"}
    data = canonical_json(manifest)
    digest, _ = write_blob(settings.oci_layout, data)
    layout = OciLayout(settings.oci_layout)
    with pytest.raises(ReleaseValidationError, match="schemaVersion"):
        verify_release(layout, settings, "amd64", digest)


def test_unsupported_manifest_media_type_rejected(settings, demo):
    manifest = json.loads(
        read_blob(settings.oci_layout, demo["versions"]["v2"]["amd64"]["digest"])
    )
    manifest["mediaType"] = "application/vnd.example.unknown+json"
    digest, _ = write_blob(settings.oci_layout, canonical_json(manifest))
    layout = OciLayout(settings.oci_layout)
    with pytest.raises(ReleaseValidationError, match="unsupported manifest mediaType"):
        verify_release(layout, settings, "amd64", digest)


def test_config_digest_mismatch_rejected(settings, demo):
    manifest = json.loads(
        read_blob(settings.oci_layout, demo["versions"]["v2"]["amd64"]["digest"])
    )
    config_path = settings.oci_layout / "blobs" / "sha256" / manifest["config"][
        "digest"
    ].split(":", 1)[1]
    config_path.write_bytes(config_path.read_bytes() + b" ")
    layout = OciLayout(settings.oci_layout)
    with pytest.raises(ReleaseValidationError, match="config digest mismatch|config descriptor size"):
        verify_release(layout, settings, "amd64", demo["versions"]["v2"]["amd64"]["digest"])


def test_descriptor_size_must_be_integer(settings, demo):
    manifest = json.loads(
        read_blob(settings.oci_layout, demo["versions"]["v2"]["amd64"]["digest"])
    )
    manifest["layers"][0]["size"] = "1024"
    digest, _ = write_blob(settings.oci_layout, canonical_json(manifest))
    layout = OciLayout(settings.oci_layout)
    with pytest.raises(ReleaseValidationError, match="non-negative integer"):
        verify_release(layout, settings, "amd64", digest)


def test_config_architecture_mismatch_rejected(settings, demo):
    layout = OciLayout(settings.oci_layout)
    with pytest.raises(ReleaseValidationError, match="architecture is amd64"):
        verify_release(layout, settings, "arm64", demo["versions"]["v2"]["amd64"]["digest"])


def test_missing_oci_layout_marker(tmp_path):
    with pytest.raises(LayoutError, match="missing oci-layout marker"):
        OciLayout(tmp_path / "nope")


def test_flat_index_json_variant_is_accepted(tmp_path):
    """A layout index.json that directly lists platform descriptors also works."""
    layout_dir = tmp_path / "layout"
    builder = LayoutBuilder(layout_dir)
    amd = builder.add_image(arch="amd64", version="v1", payload_bytes=1024)
    arm = builder.add_image(arch="arm64", version="v1", payload_bytes=1024)
    builder.write_index(preloaded=[amd, arm], ref_name="r/x:1", nested=False)

    settings = Settings.from_env(
        {"OCI_LAYOUT_DIR": str(layout_dir), "STATE_DIR": str(tmp_path / "state")}
    )
    store = fresh_store(settings)
    snapshot = store.initialize()
    assert {m["platform"]["architecture"] for m in snapshot.index["manifests"]} == {
        "amd64",
        "arm64",
    }


@pytest.mark.parametrize(
    "env, message",
    [
        ({"OCI_ARCHITECTURES": ""}, "at least one architecture"),
        ({"OCI_ARCHITECTURES": "amd64,amd64"}, "duplicates"),
        ({"OCI_ARCHITECTURES": "amd64/"}, "invalid architecture"),
        ({"LAYER_CHUNK_BYTES": "0"}, "between 1"),
        ({"LAYER_CHUNK_BYTES": "nope"}, "must be an integer"),
        ({"LAYER_CHUNK_DELAY_MS": "-1"}, "between 0 and 1000"),
        ({"OCI_INDEX_REF": "has space"}, "without spaces"),
    ],
)
def test_invalid_environment_rejected(env, message, tmp_path):
    with pytest.raises(ConfigError, match=message):
        Settings.from_env(env)


def test_environment_defaults(tmp_path):
    settings = Settings.from_env({})
    assert settings.architectures == ("amd64", "arm64")
    assert settings.layer_chunk_bytes == 64 * 1024
    assert settings.layer_chunk_delay == 0.0
    assert settings.oci_layout == Path("/oci-layout")
    assert settings.state_dir == Path("/state")


def test_digest_bytes_helper():
    assert digest_bytes(b"") == (
        "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    assert sha256_digest(b"abc").startswith("sha256:")
