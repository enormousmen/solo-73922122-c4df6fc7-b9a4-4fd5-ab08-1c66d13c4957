#!/usr/bin/env python3
"""Concurrent dual-architecture release demo.

Run against a live service (default http://127.0.0.1:8000):

    docker compose exec release-api python /srv/examples/demo_concurrent.py

The script demonstrates, in order:
  1. the preloaded stable index for both architectures,
  2. an amd64 release being verified layer-by-layer while
     - GET /v1/stable keeps answering promptly with the old amd64 digest, and
     - arm64 is released and committed independently,
  3. a stale expectedPreviousDigest producing a 409 without changing stable,
  4. the final index containing both new manifests.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_META = Path("/oci-layout/demo-digests.json")


def call(base: str, method: str, path: str, body: dict[str, Any] | None = None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={"content-type": "application/json"},
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read() or b"null")
            status = response.status
    except urllib.error.HTTPError as exc:
        payload = json.loads(exc.read() or b"null")
        status = exc.code
    return status, payload, time.monotonic() - started


def by_arch(index: dict[str, Any]) -> dict[str, str]:
    return {
        manifest["platform"]["architecture"]: manifest["digest"]
        for manifest in index["manifests"]
    }


def release(base: str, meta: dict[str, Any], version: str, arch: str, previous: str):
    return call(
        base,
        "POST",
        "/v1/releases",
        {
            "platform": f"linux/{arch}",
            "manifestDigest": meta["versions"][version][arch]["digest"],
            "expectedPreviousDigest": meta["versions"][previous][arch]["digest"],
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--meta", type=Path, default=DEFAULT_META)
    args = parser.parse_args()
    meta = json.loads(args.meta.read_text())

    status, stable, _ = call(args.base, "GET", "/v1/stable")
    assert status == 200, (status, stable)
    print("1) preloaded stable index")
    for arch, digest in by_arch(stable).items():
        print(f"   {arch}: {digest}")
    expected_baseline = {
        arch: meta["versions"]["v1"][arch]["digest"] for arch in ("amd64", "arm64")
    }
    if by_arch(stable) != expected_baseline:
        print(
            "\nnote: stable is no longer at the v1 baseline, so this demo cannot "
            "run again against the same state.\n"
            "      Reset with: docker compose down -v && docker compose up -d\n"
            "      (or point --meta at a layout whose v1 matches the live index)"
        )
        return 2

    samples: list[tuple[float, dict[str, str], float]] = []
    results: dict[str, tuple[int, Any, float]] = {}
    stop = threading.Event()
    amd_done = [0.0]

    def slow_amd64() -> None:
        results["amd64"] = release(args.base, meta, "v2", "amd64", "v1")
        amd_done[0] = time.monotonic()

    def poll_stable() -> None:
        while not stop.is_set():
            started = time.monotonic()
            code, payload, _ = call(args.base, "GET", "/v1/stable")
            finished = time.monotonic()
            if code == 200:
                samples.append((finished - started, by_arch(payload), finished))
            time.sleep(0.005)

    print("\n2) starting amd64 release; polling stable while it verifies layers")
    release_thread = threading.Thread(target=slow_amd64)
    poll_thread = threading.Thread(target=poll_stable)
    release_thread.start()
    poll_thread.start()

    time.sleep(0.05)
    arm_status, arm_payload, arm_latency = release(args.base, meta, "v2", "arm64", "v1")
    print(f"   arm64 release during amd64 scan: HTTP {arm_status} in {arm_latency:.2f}s")

    release_thread.join(timeout=60)
    stop.set()
    poll_thread.join(timeout=10)
    amd_status, amd_payload, amd_latency = results["amd64"]
    print(f"   amd64 release: HTTP {amd_status} in {amd_latency:.2f}s")

    # Only reads that completed before the amd64 commit can show the old digest;
    # later reads legitimately see v2.  Split them by that boundary.
    during = [item for item in samples if item[2] < amd_done[0]]
    slowest = max(during, key=lambda item: item[0]) if during else (0.0, {}, 0.0)
    print(
        f"   {len(during)} stable reads completed during the scan, "
        f"slowest {slowest[0]*1000:.1f}ms"
    )
    assert len(during) >= 5, len(during)
    amd_during = {item[1].get("amd64") for item in during}
    arm_during = {item[1].get("arm64") for item in during}
    assert amd_status == 200 and arm_status == 200, (amd_status, arm_status)
    assert amd_during == {meta["versions"]["v1"]["amd64"]["digest"]}, amd_during
    later = by_arch(call(args.base, "GET", "/v1/stable")[1])
    assert later["amd64"] == meta["versions"]["v2"]["amd64"]["digest"], later
    assert later["arm64"] == meta["versions"]["v2"]["arm64"]["digest"], later
    print("   invariant: amd64 stayed on v1 throughout its scan; arm64 moved to v2")
    print(f"   arm64 digests observed during scan: {sorted(arm_during)}")

    print("\n3) stale baseline conflict")
    conflict_status, conflict_payload, _ = release(args.base, meta, "v3", "amd64", "v1")
    print(f"   amd64 v3 with stale baseline: HTTP {conflict_status}")
    assert conflict_status == 409, conflict_payload
    print(f"   current amd64 digest reported: {conflict_payload['error']['details']['currentDigest']}")
    unchanged = by_arch(call(args.base, "GET", "/v1/stable")[1])
    assert unchanged == later, unchanged
    assert call(args.base, "GET", "/v1/stable")[1]["manifests"], "index vanished"
    print("   stable index unchanged after conflict")

    print("\n4) fresh baseline amd64 v3 release")
    fresh_status, fresh_payload, _ = release(args.base, meta, "v3", "amd64", "v2")
    print(f"   HTTP {fresh_status}, revision {fresh_payload.get('revision')}")
    assert fresh_status == 200, fresh_payload
    final = by_arch(fresh_payload["stable"])
    assert len(fresh_payload["stable"]["manifests"]) == 2
    assert final["amd64"] == meta["versions"]["v3"]["amd64"]["digest"]
    assert final["arm64"] == meta["versions"]["v2"]["arm64"]["digest"]
    print("   final index:")
    for arch, digest in final.items():
        print(f"     {arch}: {digest}")
    print("\ndemo complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
