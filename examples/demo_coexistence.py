#!/usr/bin/env python3
"""Coexistence demo: did the amd64 and arm64 images ever share a stable line?

Run against a live service (default http://127.0.0.1:8000):

    docker compose exec release-api python /srv/examples/demo_coexistence.py

The script drives the "split, then restore the same pair" scenario:

  rev 0  (enablement)  amd64 v1 + arm64 v1
  rev 1  amd64 -> v2   (pair v2/v1, not aligned)
  rev 2  arm64 -> v2   (pair v2/v2, aligned for the first time)
  rev 3  amd64 -> v3   (pair v3/v2, split)
  rev 4  amd64 -> v2   (pair v2/v2 restored -> a second, separate interval)

It then queries POST /v1/stable/coexistence for the v2/v2 digest pair and
shows both intervals plus the covered revision range. A stale-baseline 409 and
an unknown-digest query demonstrate that conflicts and never-published digests
do not create history.
"""

from __future__ import annotations

import argparse
import json
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
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


def digest(meta: dict[str, Any], version: str, arch: str) -> str:
    return meta["versions"][version][arch]["digest"]


def release(base: str, meta: dict[str, Any], version: str, arch: str, previous: str):
    return call(
        base,
        "POST",
        "/v1/releases",
        {
            "platform": f"linux/{arch}",
            "manifestDigest": digest(meta, version, arch),
            "expectedPreviousDigest": digest(meta, previous, arch),
        },
    )


def coexistence(base: str, pair: dict[str, str]) -> dict[str, Any]:
    status, payload = call(
        base,
        "POST",
        "/v1/stable/coexistence",
        {
            "platforms": [
                {"platform": f"linux/{arch}", "manifestDigest": pair_digest}
                for arch, pair_digest in sorted(pair.items())
            ]
        },
    )
    assert status == 200, (status, payload)
    return payload


def print_intervals(title: str, body: dict[str, Any]) -> None:
    coverage = body["coverage"]
    print(f"\n{title}")
    print(
        "   covered revisions: "
        f"{coverage['firstRevision']}..{coverage['lastRevision']} "
        f"(earlier unknown: {coverage['earlierRevisionsUnknown']}, "
        f"ephemeral: {coverage['historyEphemeral']})"
    )
    if not body["intervals"]:
        print("   no interval: the submitted digests were never active together")
        return
    for interval in body["intervals"]:
        flags = []
        if interval["touchesHistoryStart"]:
            flags.append("start may predate known history")
        if interval["active"]:
            flags.append("still active")
        suffix = f" [{'; '.join(flags)}]" if flags else ""
        print(
            f"   - revisions {interval['startRevision']}..{interval['endRevision']} "
            f"({interval['revisions']} revision(s), "
            f"{interval['startRecordKind']} -> {interval['endRecordKind']}){suffix}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--meta", type=Path, default=DEFAULT_META)
    args = parser.parse_args()
    meta = json.loads(args.meta.read_text())

    status, stable = call(args.base, "GET", "/v1/stable")
    assert status == 200, (status, stable)
    current = {m["platform"]["architecture"]: m["digest"] for m in stable["manifests"]}
    baseline = {arch: digest(meta, "v1", arch) for arch in ("amd64", "arm64")}
    if current != baseline:
        print(
            "\nnote: stable is no longer at the v1 baseline, so this demo cannot "
            "run again against the same state.\n"
            "      Reset with: docker compose down -v && docker compose up -d"
        )
        return 2

    print("1) initial coexistence check on the preloaded v1/v1 pair")
    body = coexistence(
        args.base,
        {"amd64": digest(meta, "v1", "amd64"), "arm64": digest(meta, "v1", "arm64")},
    )
    print_intervals("v1/v1 pair:", body)
    assert [(i["startRevision"], i["endRevision"]) for i in body["intervals"]] == [(0, 0)]

    print("\n2) driving releases: align on v2, split amd64 to v3, restore v2")
    steps = [
        ("v2", "amd64", "v1"),
        ("v2", "arm64", "v1"),
        ("v3", "amd64", "v2"),
        ("v2", "amd64", "v3"),
    ]
    for version, arch, previous in steps:
        status, payload = release(args.base, meta, version, arch, previous)
        assert status == 200, (status, payload)
        print(f"   released linux/{arch} {version}: revision {payload['revision']}")

    print("\n3) query the v2/v2 pair again: split and restore -> two intervals")
    pair_v2 = {
        "amd64": digest(meta, "v2", "amd64"),
        "arm64": digest(meta, "v2", "arm64"),
    }
    body = coexistence(args.base, pair_v2)
    print_intervals("v2/v2 pair:", body)
    ranges = [(i["startRevision"], i["endRevision"]) for i in body["intervals"]]
    assert ranges == [(2, 2), (4, 4)], ranges
    assert body["intervals"][-1]["active"] is True
    assert body["coverage"]["lastRevision"] == 4

    print("\n4) a pair that was never aligned returns no interval")
    never = {
        "amd64": digest(meta, "v3", "amd64"),
        "arm64": digest(meta, "v1", "arm64"),
    }
    body = coexistence(args.base, never)
    print_intervals("v3/v1 pair:", body)
    assert body["intervals"] == []

    print("\n5) a conflict (409) must not add a revision to history")
    status, payload = release(args.base, meta, "v3", "arm64", "v1")
    print(f"   stale baseline release: HTTP {status}")
    assert status == 409, payload
    body = coexistence(args.base, pair_v2)
    assert body["coverage"]["lastRevision"] == 4
    print(f"   coverage still ends at revision {body['coverage']['lastRevision']}")

    print("\n6) validation: malformed request is rejected without a history write")
    status, payload = call(
        args.base,
        "POST",
        "/v1/stable/coexistence",
        {"platforms": [{"platform": "linux/s390x", "manifestDigest": "sha256:" + "0" * 64}]},
    )
    print(f"   HTTP {status}: {payload['error']['code']}")
    assert status == 422

    status, health = call(args.base, "GET", "/healthz")
    print(
        "\nhealthz history:",
        json.dumps(health["history"], sort_keys=True),
    )
    print("\ndemo complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
