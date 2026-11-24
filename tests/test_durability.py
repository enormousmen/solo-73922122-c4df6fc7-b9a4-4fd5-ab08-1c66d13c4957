"""Durability: failed releases and restarts keep the last success queryable."""

from __future__ import annotations

import json
import stat

import pytest

from app.config import Settings
from app.oci import LayoutError
from app.store import StableStore
from tests.conftest import LayoutBuilder, fresh_store, make_client


def _release(client, demo, version: str, arch: str, previous: str = "v1"):
    return client.post(
        "/v1/releases",
        json={
            "platform": f"linux/{arch}",
            "manifestDigest": demo["versions"][version][arch]["digest"],
            "expectedPreviousDigest": demo["versions"][previous][arch]["digest"],
        },
    )


def test_failed_release_does_not_change_stable(settings, demo):
    with make_client(settings) as client:
        assert _release(client, demo, "v2", "amd64").status_code == 200
        good = client.get("/v1/stable").json()

        # v3 amd64 manifest exists, but ask with a stale baseline -> 409.
        conflict = client.post(
            "/v1/releases",
            json={
                "platform": "linux/amd64",
                "manifestDigest": demo["versions"]["v3"]["amd64"]["digest"],
                "expectedPreviousDigest": demo["versions"]["v1"]["amd64"]["digest"],
            },
        )
    assert conflict.status_code == 409

    # New process, same state dir: the last success is what we see.
    with make_client(settings) as restarted:
        assert restarted.get("/v1/stable").json() == good
        assert restarted.get("/v1/stable").headers["x-index-revision"] == "1"


def test_restart_restores_published_state(settings, demo):
    with make_client(settings) as client:
        assert _release(client, demo, "v2", "amd64").status_code == 200
        assert _release(client, demo, "v3", "arm64", previous="v1").status_code == 200
        expected = client.get("/v1/stable").json()

    with make_client(settings) as restarted:
        assert restarted.get("/v1/stable").json() == expected
        history = restarted.get("/v1/releases").json()
        assert history["revision"] == 2
        assert history["published"]["amd64"]["digest"] == (
            demo["versions"]["v2"]["amd64"]["digest"]
        )
        assert history["published"]["arm64"]["digest"] == (
            demo["versions"]["v3"]["arm64"]["digest"]
        )


def test_conflict_after_restart_against_persisted_baseline(settings, demo):
    with make_client(settings) as client:
        assert _release(client, demo, "v2", "amd64").status_code == 200

    with make_client(settings) as restarted:
        stale = restarted.post(
            "/v1/releases",
            json={
                "platform": "linux/amd64",
                "manifestDigest": demo["versions"]["v3"]["amd64"]["digest"],
                "expectedPreviousDigest": demo["versions"]["v1"]["amd64"]["digest"],
            },
        )
        assert stale.status_code == 409
        fresh = restarted.post(
            "/v1/releases",
            json={
                "platform": "linux/amd64",
                "manifestDigest": demo["versions"]["v3"]["amd64"]["digest"],
                "expectedPreviousDigest": demo["versions"]["v2"]["amd64"]["digest"],
            },
        )
        assert fresh.status_code == 200


def test_state_file_is_atomic_and_complete(settings, demo):
    with make_client(settings) as client:
        assert _release(client, demo, "v2", "arm64").status_code == 200

    state = json.loads((settings.state_dir / "stable-index.json").read_text())
    assert state["schemaVersion"] == 1
    assert state["revision"] == 1
    assert state["ref"] == settings.index_ref
    assert set(state["published"]) == {"arm64"}
    assert len(state["index"]["manifests"]) == 2
    # No temp files left behind.
    leftovers = [p for p in settings.state_dir.iterdir() if p.name.startswith(".stable-index")]
    assert leftovers == []


def test_corrupt_state_file_fails_startup_instead_of_resetting(settings, demo):
    """State is the source of truth; a corrupt file must not be silently replaced."""
    from app.store import StoreError

    with make_client(settings) as client:
        assert _release(client, demo, "v2", "amd64").status_code == 200
    (settings.state_dir / "stable-index.json").write_text("{not json")

    with pytest.raises(StoreError, match="cannot parse"):
        with make_client(settings):
            pass
    # The bad file is left in place for an operator, not overwritten.
    assert (settings.state_dir / "stable-index.json").read_text() == "{not json"


def test_read_only_state_dir_still_serves_preloaded_index(tmp_path, demo):
    layout_dir = tmp_path / "oci-layout"
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
    settings = Settings.from_env(
        {"OCI_LAYOUT_DIR": str(layout_dir), "STATE_DIR": str(state_dir)}
    )
    try:
        with make_client(settings) as client:
            stable = client.get("/v1/stable")
            assert stable.status_code == 200
            assert len(stable.json()["manifests"]) == 2
            assert stable.headers["x-index-revision"] == "0"
            health = client.get("/healthz").json()
            assert health["persisted"] is False

            rejected = _release(client, demo, "v2", "amd64")
            assert rejected.status_code in (409, 503)
    finally:
        state_dir.chmod(stat.S_IRWXU)


def test_missing_layout_fails_startup(tmp_path):
    settings = Settings.from_env(
        {"OCI_LAYOUT_DIR": str(tmp_path / "absent"), "STATE_DIR": str(tmp_path / "state")}
    )
    with pytest.raises(LayoutError):
        # create_app constructs the layout eagerly.
        from app.main import create_app

        create_app(settings)


def test_preloaded_index_missing_an_architecture_rejected(tmp_path):
    layout_dir = tmp_path / "layout"
    builder = LayoutBuilder(layout_dir)
    amd = builder.add_image(arch="amd64", version="v1", payload_bytes=1024)
    builder.write_index(preloaded=[amd], ref_name="r/x:1")
    settings = Settings.from_env(
        {"OCI_LAYOUT_DIR": str(layout_dir), "STATE_DIR": str(tmp_path / "state")}
    )
    store: StableStore = fresh_store(settings)
    with pytest.raises(LayoutError, match="missing configured architectures"):
        store.initialize()


def test_preloaded_index_with_missing_manifest_blob_rejected(tmp_path):
    layout_dir = tmp_path / "layout"
    builder = LayoutBuilder(layout_dir)
    amd = builder.add_image(arch="amd64", version="v1", payload_bytes=1024)
    arm = builder.add_image(arch="arm64", version="v1", payload_bytes=1024)
    builder.write_index(preloaded=[amd, arm], ref_name="r/x:1")
    # Remove arm64 manifest blob but leave the descriptor.
    arm_blob = layout_dir / "blobs" / "sha256" / arm["digest"].split(":", 1)[1]
    arm_blob.unlink()

    settings = Settings.from_env(
        {"OCI_LAYOUT_DIR": str(layout_dir), "STATE_DIR": str(tmp_path / "state")}
    )
    with pytest.raises(LayoutError, match="missing from the layout"):
        fresh_store(settings).initialize()


def test_state_persists_only_after_full_verification(settings, demo, monkeypatch):
    """A failure mid-scan must leave the state file untouched."""
    with make_client(settings) as client:
        before = (settings.state_dir / "stable-index.json").read_text()

    from app import oci

    original = oci.verify_release

    def boom(*args, **kwargs):
        raise oci.ReleaseValidationError("simulated crash", code="invalid_release")

    monkeypatch.setattr("app.main.verify_release", boom, raising=True)
    with make_client(settings) as client:
        response = _release(client, demo, "v2", "amd64")
        assert response.status_code == 422
    monkeypatch.setattr("app.main.verify_release", original, raising=True)
    assert (settings.state_dir / "stable-index.json").read_text() == before
