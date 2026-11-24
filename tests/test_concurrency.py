"""Concurrency: a slow layer scan must not block reads or the other arch."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app.config import Settings
from tests.conftest import LayoutBuilder, make_client, stable_by_arch


@pytest.fixture
def concurrent_setup(tmp_path: Path):
    layout_dir = tmp_path / "layout"
    builder = LayoutBuilder(layout_dir)
    amd_v1 = builder.add_image(arch="amd64", version="v1", payload_bytes=8 * 1024)
    arm_v1 = builder.add_image(arch="arm64", version="v1", payload_bytes=8 * 1024)
    # amd64's candidate is deliberately much larger: its scan is the slow one.
    amd_v2 = builder.add_image(arch="amd64", version="v2", payload_bytes=256 * 1024)
    arm_v2 = builder.add_image(arch="arm64", version="v2", payload_bytes=8 * 1024)
    builder.write_index(preloaded=[amd_v1, arm_v1], ref_name="registry.example.com/edge/stable:latest")

    settings = Settings.from_env(
        {
            "OCI_LAYOUT_DIR": str(layout_dir),
            "STATE_DIR": str(tmp_path / "state"),
            "LAYER_CHUNK_BYTES": "512",
            "LAYER_CHUNK_DELAY_MS": "2",
        }
    )
    return {
        "settings": settings,
        "amd_v1": amd_v1,
        "arm_v1": arm_v1,
        "amd_v2": amd_v2,
        "arm_v2": arm_v2,
    }


def test_slow_scan_does_not_block_reads_or_other_architecture(concurrent_setup):
    setup = concurrent_setup
    settings = setup["settings"]
    amd_body = {
        "platform": "linux/amd64",
        "manifestDigest": setup["amd_v2"]["digest"],
        "expectedPreviousDigest": setup["amd_v1"]["digest"],
    }
    arm_body = {
        "platform": "linux/arm64",
        "manifestDigest": setup["arm_v2"]["digest"],
        "expectedPreviousDigest": setup["arm_v1"]["digest"],
    }

    results: dict[str, object] = {}
    timings: dict[str, float] = {}
    # One app instance: the active-release registry is in-process, so this is
    # where we can observe the scan in flight.  Request handling is threaded.
    with make_client(settings) as client:
        def do_slow_release() -> None:
            started = time.monotonic()
            results["amd"] = client.post("/v1/releases", json=amd_body)
            timings["amd"] = time.monotonic() - started

        thread = threading.Thread(target=do_slow_release)
        thread.start()

        # Wait until the slow release is actually scanning.
        deadline = time.monotonic() + 10
        active = []
        while time.monotonic() < deadline:
            active = client.get("/healthz").json().get("activeReleases", [])
            if any(item["platform"] == "linux/amd64" for item in active):
                break
            time.sleep(0.01)
        assert any(item["platform"] == "linux/amd64" for item in active), active

        # Reads stay fast and still serve the pre-release index.
        started = time.monotonic()
        stable = client.get("/v1/stable")
        stable_latency = time.monotonic() - started
        assert stable.status_code == 200
        assert stable_latency < 0.5, stable_latency
        by_arch = stable_by_arch(stable.json())
        assert by_arch["amd64"]["digest"] == setup["amd_v1"]["digest"]
        assert by_arch["arm64"]["digest"] == setup["arm_v1"]["digest"]

        # The other architecture completes while the slow scan is in flight.
        started = time.monotonic()
        arm_response = client.post("/v1/releases", json=arm_body)
        arm_latency = time.monotonic() - started
        assert arm_response.status_code == 200, arm_response.text
        assert arm_latency < 0.5, arm_latency
        assert thread.is_alive(), "slow release should still be verifying"


        # While the slow release is in flight the arm64 update is visible but
        # amd64 still shows its previous digest (no partial swap).
        mid = stable_by_arch(client.get("/v1/stable").json())
        assert mid["arm64"]["digest"] == setup["arm_v2"]["digest"]
        assert mid["amd64"]["digest"] == setup["amd_v1"]["digest"]

        thread.join(timeout=30)
        assert not thread.is_alive()
        assert results["amd"].status_code == 200, results["amd"].text
        # The slow scan really was slow, so the overlap above was not a fluke.
        assert timings["amd"] > 1.0, timings
        assert arm_latency < timings["amd"] / 2, (arm_latency, timings)

        final = stable_by_arch(client.get("/v1/stable").json())
        assert final["amd64"]["digest"] == setup["amd_v2"]["digest"]
        assert final["arm64"]["digest"] == setup["arm_v2"]["digest"]
        assert client.get("/healthz").json()["activeReleases"] == []


def test_concurrent_same_architecture_has_one_winner(concurrent_setup):
    setup = concurrent_setup
    settings = setup["settings"]
    body = {
        "platform": "linux/amd64",
        "manifestDigest": setup["amd_v2"]["digest"],
        "expectedPreviousDigest": setup["amd_v1"]["digest"],
    }
    barrier = threading.Barrier(2)
    statuses: list[int] = []
    lock = threading.Lock()

    with make_client(settings) as first, make_client(settings) as second:
        def release(client) -> None:
            barrier.wait()
            response = client.post("/v1/releases", json=body)
            with lock:
                statuses.append(response.status_code)

        threads = [
            threading.Thread(target=release, args=(first,)),
            threading.Thread(target=release, args=(second,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

    assert sorted(statuses) == [200, 409], statuses
    with make_client(settings) as client:
        assert client.get("/v1/stable").headers["x-index-revision"] == "1"


def test_two_processes_committing_different_architectures_both_survive(concurrent_setup):
    setup = concurrent_setup
    settings = setup["settings"]
    bodies = {
        "amd64": {
            "platform": "linux/amd64",
            "manifestDigest": setup["amd_v2"]["digest"],
            "expectedPreviousDigest": setup["amd_v1"]["digest"],
        },
        "arm64": {
            "platform": "linux/arm64",
            "manifestDigest": setup["arm_v2"]["digest"],
            "expectedPreviousDigest": setup["arm_v1"]["digest"],
        },
    }
    # Two independent app instances share only the state directory, which is
    # how separate worker processes / containers would behave.
    clients = {arch: make_client(settings) for arch in bodies}
    for client in clients.values():
        client.__enter__()
    try:
        barrier = threading.Barrier(2)
        responses: dict[str, int] = {}

        def release(arch: str) -> None:
            barrier.wait()
            responses[arch] = clients[arch].post("/v1/releases", json=bodies[arch]).status_code

        threads = [threading.Thread(target=release, args=(arch,)) for arch in bodies]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert responses == {"amd64": 200, "arm64": 200}, responses
        final = stable_by_arch(clients["amd64"].get("/v1/stable").json())
        assert final["amd64"]["digest"] == setup["amd_v2"]["digest"]
        assert final["arm64"]["digest"] == setup["arm_v2"]["digest"]
        assert clients["amd64"].get("/v1/stable").headers["x-index-revision"] == "2"
    finally:
        for client in clients.values():
            client.__exit__(None, None, None)


def test_separate_os_processes_serialise_commits(concurrent_setup):
    """Real cross-process commit: two interpreters share only /state."""
    setup = concurrent_setup
    settings = setup["settings"]
    script = """
import json, sys
from app.config import Settings
from app.oci import OciLayout, verify_release
from app.store import StableStore

settings = Settings.from_env(json.loads(sys.argv[1]))
arch = sys.argv[2]
manifest = sys.argv[3]
previous = sys.argv[4]
layout = OciLayout(settings.oci_layout)
store = StableStore(settings, layout)
store.initialize()
verified = verify_release(layout, settings, arch, manifest)
snapshot, record = store.commit(verified, previous)
print(json.dumps({"revision": snapshot.revision, "arch": arch}))
"""
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    payload = json.dumps(
        {
            "OCI_LAYOUT_DIR": str(settings.oci_layout),
            "STATE_DIR": str(settings.state_dir),
            "LAYER_CHUNK_BYTES": str(settings.layer_chunk_bytes),
            "LAYER_CHUNK_DELAY_MS": "2",
        }
    )
    bodies = {
        "amd64": (setup["amd_v2"]["digest"], setup["amd_v1"]["digest"]),
        "arm64": (setup["arm_v2"]["digest"], setup["arm_v1"]["digest"]),
    }
    procs = {
        arch: subprocess.Popen(
            [sys.executable, "-c", script, payload, arch, new, old],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
        )
        for arch, (new, old) in bodies.items()
    }
    outputs = {}
    for arch, proc in procs.items():
        out, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, (arch, err)
        outputs[arch] = json.loads(out)["revision"]

    assert sorted(outputs.values()) == [1, 2], outputs
    with make_client(settings) as client:
        final = stable_by_arch(client.get("/v1/stable").json())
        assert final["amd64"]["digest"] == setup["amd_v2"]["digest"]
        assert final["arm64"]["digest"] == setup["arm_v2"]["digest"]
        assert client.get("/v1/stable").headers["x-index-revision"] == "2"
