import json

import pytest

from tests.conftest import (
    LayoutBuilder,
    append_garbage_to_layer,
    read_blob,
    rewrite_manifest_size,
    rewrite_manifest_with_bad_layer_digest,
    stable_by_arch,
)


def test_stable_returns_full_preloaded_index(client, demo, settings):
    response = client.get("/v1/stable")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(
        "application/vnd.oci.image.index.v1+json"
    )
    body = response.json()
    assert body["schemaVersion"] == 2
    assert body["mediaType"] == "application/vnd.oci.image.index.v1+json"
    by_arch = stable_by_arch(body)
    assert set(by_arch) == {"amd64", "arm64"}
    for arch in ("amd64", "arm64"):
        assert by_arch[arch]["digest"] == demo["versions"]["v1"][arch]["digest"]
    assert response.headers["x-index-revision"] == "0"
    assert "x-index-updated-at" not in response.headers


def test_stable_etag_short_circuits(client):
    first = client.get("/v1/stable")
    etag = first.headers["etag"]
    second = client.get("/v1/stable", headers={"If-None-Match": etag})
    assert second.status_code == 304
    assert second.content == b""


def test_single_architecture_release_preserves_other(client, demo):
    stable_before = client.get("/v1/stable").json()
    other_before = stable_by_arch(stable_before)["arm64"]

    body = {
        "platform": "linux/amd64",
        "manifestDigest": demo["versions"]["v2"]["amd64"]["digest"],
        "expectedPreviousDigest": demo["versions"]["v1"]["amd64"]["digest"],
    }
    response = client.post("/v1/releases", json=body)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["released"]["platform"] == "linux/amd64"
    assert payload["released"]["previousDigest"] == body["expectedPreviousDigest"]
    assert payload["released"]["verified"]["layers"] == 2
    assert payload["released"]["verified"]["chunks"] >= 2

    stable_after = client.get("/v1/stable").json()
    by_arch = stable_by_arch(stable_after)
    assert set(by_arch) == {"amd64", "arm64"}
    assert by_arch["amd64"]["digest"] == demo["versions"]["v2"]["amd64"]["digest"]
    assert by_arch["arm64"] == other_before
    assert len(stable_after["manifests"]) == 2


def test_both_architectures_release_independently(client, demo):
    for arch in ("amd64", "arm64"):
        response = client.post(
            "/v1/releases",
            json={
                "platform": f"linux/{arch}",
                "manifestDigest": demo["versions"]["v2"][arch]["digest"],
                "expectedPreviousDigest": demo["versions"]["v1"][arch]["digest"],
            },
        )
        assert response.status_code == 200, response.text

    by_arch = stable_by_arch(client.get("/v1/stable").json())
    assert by_arch["amd64"]["digest"] == demo["versions"]["v2"]["amd64"]["digest"]
    assert by_arch["arm64"]["digest"] == demo["versions"]["v2"]["arm64"]["digest"]
    assert client.get("/v1/stable").headers["x-index-revision"] == "2"


def test_conflict_when_expected_digest_is_stale(client, demo):
    body = {
        "platform": "linux/amd64",
        "manifestDigest": demo["versions"]["v2"]["amd64"]["digest"],
        "expectedPreviousDigest": demo["versions"]["v1"]["amd64"]["digest"],
    }
    assert client.post("/v1/releases", json=body).status_code == 200
    before_conflict = client.get("/v1/stable").json()

    retry = client.post(
        "/v1/releases",
        json={**body, "manifestDigest": demo["versions"]["v3"]["amd64"]["digest"]},
    )
    assert retry.status_code == 409
    error = retry.json()["error"]
    assert error["code"] == "conflict"
    assert error["details"]["currentDigest"] == demo["versions"]["v2"]["amd64"]["digest"]

    after_conflict = client.get("/v1/stable").json()
    assert after_conflict == before_conflict
    assert client.get("/v1/stable").headers["x-index-revision"] == "1"


def test_conflict_for_other_architecture_leaves_it_untouched(client, demo):
    body = {
        "platform": "linux/arm64",
        "manifestDigest": demo["versions"]["v2"]["arm64"]["digest"],
        "expectedPreviousDigest": demo["versions"]["v1"]["arm64"]["digest"],
    }
    assert client.post("/v1/releases", json=body).status_code == 200
    before = client.get("/v1/stable").json()

    conflict = client.post(
        "/v1/releases",
        json={**body, "manifestDigest": demo["versions"]["v3"]["arm64"]["digest"]},
    )
    assert conflict.status_code == 409
    assert client.get("/v1/stable").json() == before


def test_unknown_manifest_digest_rejected(client):
    response = client.post(
        "/v1/releases",
        json={
            "platform": "linux/amd64",
            "manifestDigest": "sha256:" + "a" * 64,
            "expectedPreviousDigest": "sha256:" + "b" * 64,
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "blob_not_found"


def test_manifest_digest_must_match_blob_content(client, demo, settings):
    # Point at a blob that exists but is not the manifest we claim.
    manifest = json.loads(
        read_blob(settings.oci_layout, demo["versions"]["v2"]["amd64"]["digest"])
    )
    config_digest = manifest["config"]["digest"]
    response = client.post(
        "/v1/releases",
        json={
            "platform": "linux/amd64",
            "manifestDigest": config_digest,
            "expectedPreviousDigest": demo["versions"]["v1"]["amd64"]["digest"],
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_manifest"


@pytest.mark.parametrize(
    "mutation",
    ["bad_layer_digest", "bad_config_size", "garbage_layer"],
)
def test_corrupt_blobs_rejected(client, demo, settings, mutation):
    manifest_digest = demo["versions"]["v2"]["amd64"]["digest"]
    if mutation == "bad_layer_digest":
        manifest_digest = rewrite_manifest_with_bad_layer_digest(
            settings.oci_layout, manifest_digest
        )
    elif mutation == "bad_config_size":
        manifest_digest = rewrite_manifest_size(settings.oci_layout, manifest_digest, 1)
    else:
        append_garbage_to_layer(settings.oci_layout, manifest_digest)

    response = client.post(
        "/v1/releases",
        json={
            "platform": "linux/amd64",
            "manifestDigest": manifest_digest,
            "expectedPreviousDigest": demo["versions"]["v1"]["amd64"]["digest"],
        },
    )
    assert response.status_code == 422, response.text
    by_arch = stable_by_arch(client.get("/v1/stable").json())
    assert by_arch["amd64"]["digest"] == demo["versions"]["v1"]["amd64"]["digest"]


def test_platform_mismatch_between_request_and_config(client, demo):
    # amd64 manifest submitted as linux/arm64.
    response = client.post(
        "/v1/releases",
        json={
            "platform": "linux/arm64",
            "manifestDigest": demo["versions"]["v2"]["amd64"]["digest"],
            "expectedPreviousDigest": demo["versions"]["v1"]["arm64"]["digest"],
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "platform_mismatch"


def test_config_platform_must_match_descriptor_on_startup(tmp_path):
    """A preloaded index whose descriptor lies about the platform is rejected."""
    from app.config import Settings
    from app.oci import LayoutError
    from tests.conftest import fresh_store

    layout_dir = tmp_path / "layout"
    builder = LayoutBuilder(layout_dir)
    amd = builder.add_image(arch="amd64", version="v1", payload_bytes=1024)
    arm = builder.add_image(arch="arm64", version="v1", payload_bytes=1024)
    arm["platform"]["architecture"] = "amd64"  # lie: duplicate amd64
    builder.write_index(preloaded=[amd, arm], ref_name="r/x:1")
    settings = Settings.from_env(
        {"OCI_LAYOUT_DIR": str(layout_dir), "STATE_DIR": str(tmp_path / "state")}
    )
    store = fresh_store(settings)
    with pytest.raises(LayoutError, match="duplicate platform"):
        store.initialize()


def test_unconfigured_platform_rejected(client, demo):
    response = client.post(
        "/v1/releases",
        json={
            "platform": "linux/s390x",
            "manifestDigest": demo["versions"]["v2"]["amd64"]["digest"],
            "expectedPreviousDigest": demo["versions"]["v1"]["amd64"]["digest"],
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_platform"


def test_request_validation_errors_are_structured(client):
    response = client.post(
        "/v1/releases",
        json={"platform": "linux/amd64", "manifestDigest": "not-a-digest"},
    )
    assert response.status_code == 422
    assert "error" in response.json() or "detail" in response.json()


def test_platform_endpoint_returns_single_descriptor(client, demo):
    response = client.get("/v1/stable/amd64")
    assert response.status_code == 200
    body = response.json()
    assert body["platform"] == {"architecture": "amd64", "os": "linux"}
    assert body["digest"] == demo["versions"]["v1"]["amd64"]["digest"]

    assert client.get("/v1/stable/riscv64").status_code == 404
    assert client.get("/v1/stable/s390x").status_code == 404


def test_release_keeps_manifest_annotations_in_descriptor(client, demo):
    response = client.post(
        "/v1/releases",
        json={
            "platform": "linux/arm64",
            "manifestDigest": demo["versions"]["v3"]["arm64"]["digest"],
            "expectedPreviousDigest": demo["versions"]["v1"]["arm64"]["digest"],
        },
    )
    assert response.status_code == 200, response.text
    descriptor = response.json()["released"]["descriptor"]
    assert descriptor["annotations"]["org.opencontainers.image.version"] == "v3"
    stored = stable_by_arch(client.get("/v1/stable").json())["arm64"]
    assert stored["annotations"]["org.opencontainers.image.version"] == "v3"


def test_release_response_reports_verified_sizes(client, demo, settings):
    response = client.post(
        "/v1/releases",
        json={
            "platform": "linux/amd64",
            "manifestDigest": demo["versions"]["v3"]["amd64"]["digest"],
            "expectedPreviousDigest": demo["versions"]["v1"]["amd64"]["digest"],
        },
    )
    assert response.status_code == 200
    verified = response.json()["released"]["verified"]
    manifest = json.loads(
        read_blob(settings.oci_layout, demo["versions"]["v3"]["amd64"]["digest"])
    )
    assert verified["layers"] == len(manifest["layers"])
    assert verified["layerBytes"] == sum(layer["size"] for layer in manifest["layers"])
    assert verified["configBytes"] == manifest["config"]["size"]
    assert verified["chunks"] >= verified["layers"]


def test_history_endpoint_tracks_last_release_per_arch(client, demo):
    assert client.get("/v1/releases").json()["published"] == {}
    for arch in ("amd64", "arm64"):
        client.post(
            "/v1/releases",
            json={
                "platform": f"linux/{arch}",
                "manifestDigest": demo["versions"]["v2"][arch]["digest"],
                "expectedPreviousDigest": demo["versions"]["v1"][arch]["digest"],
            },
        )
    history = client.get("/v1/releases").json()
    assert history["revision"] == 2
    assert set(history["published"]) == {"amd64", "arm64"}
    assert history["published"]["amd64"]["previousDigest"] == (
        demo["versions"]["v1"]["amd64"]["digest"]
    )
    assert history["active"] == []
