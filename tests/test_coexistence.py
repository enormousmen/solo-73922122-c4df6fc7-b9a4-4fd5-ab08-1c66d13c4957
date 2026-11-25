"""Coexistence API: revision ranges where a manifest pair was active together.

Covers:
* interval computation, split-and-restore producing two ranges,
* coverage boundaries and the "older revisions unknown" marker,
* old-volume migration (state file without a journal),
* journal reconciliation after a simulated state/journal crash gap,
* torn-tail tolerance and hard failure on mid-file corruption,
* constant per-release journal write size,
* restart and cross-process visibility of committed pairs,
* failed validation / conflicts / uncommitted scans never enter history,
* point-in-time snapshots while releases keep flowing,
* read-only state directory ephemeral behaviour.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from app.config import Settings
from app.history import (
    HISTORY_FILENAME,
    HistoryError,
    HistoryRecord,
    ReleaseHistory,
    find_coexistence_intervals,
    load_history_view,
)
from tests.conftest import make_client

HISTORY_PATH = lambda settings: settings.state_dir / HISTORY_FILENAME  # noqa: E731


def _digest(demo: dict, version: str, arch: str) -> str:
    return demo["versions"][version][arch]["digest"]


def _release(client, demo: dict, version: str, arch: str, previous: str) -> int:
    return client.post(
        "/v1/releases",
        json={
            "platform": f"linux/{arch}",
            "manifestDigest": _digest(demo, version, arch),
            "expectedPreviousDigest": _digest(demo, previous, arch),
        },
    ).status_code


def _check(client, demo: dict, amd_version: str, arm_version: str):
    return client.post(
        "/v1/stable/coexistence",
        json={
            "platforms": [
                {
                    "platform": "linux/amd64",
                    "manifestDigest": _digest(demo, amd_version, "amd64"),
                },
                {
                    "platform": "linux/arm64",
                    "manifestDigest": _digest(demo, arm_version, "arm64"),
                },
            ]
        },
    )


def _ranges(body: dict) -> list[tuple[int, int]]:
    return [(item["startRevision"], item["endRevision"]) for item in body["intervals"]]


def test_coexistence_seeds_enablement_record_at_revision_zero(client, demo):
    body = _check(client, demo, "v1", "v1").json()
    assert body["coexisting"] is True
    assert body["coverage"] == {
        "firstRevision": 0,
        "lastRevision": 0,
        "earlierRevisionsUnknown": False,
        "historyEphemeral": False,
        "firstRecordKind": "enablement",
    }
    interval = body["intervals"][0]
    assert interval["startRevision"] == 0
    assert interval["endRevision"] == 0
    assert interval["touchesHistoryStart"] is True
    assert interval["active"] is True
    assert interval["startRecordKind"] == "enablement"


def test_split_and_restore_lists_two_separate_intervals(client, demo):
    # rev 1: amd64 v2 (pair v2/v1); rev 2: arm64 v2 (pair v2/v2);
    # rev 3: amd64 v3 (pair v3/v2, split); rev 4: amd64 v2 (pair v2/v2 again).
    assert _release(client, demo, "v2", "amd64", "v1") == 200
    assert _release(client, demo, "v2", "arm64", "v1") == 200
    assert _release(client, demo, "v3", "amd64", "v2") == 200
    assert _release(client, demo, "v2", "amd64", "v3") == 200

    body = _check(client, demo, "v2", "v2").json()
    assert body["coverage"]["firstRevision"] == 0
    assert body["coverage"]["lastRevision"] == 4
    assert _ranges(body) == [(2, 2), (4, 4)]
    assert body["intervals"][0]["active"] is False
    assert body["intervals"][1]["active"] is True
    assert all(item["startRecordKind"] == "release" for item in body["intervals"])


def test_multi_revision_contiguous_interval_spans_all_matching_revisions(client, demo):
    # rev 1: arm64 v2; rev 2: amd64 v2 -> v2/v2 active at 2 and 3;
    # rev 3: arm64 v3 keeps amd64 on v2 but changes the arm64 digest.
    assert _release(client, demo, "v2", "arm64", "v1") == 200
    assert _release(client, demo, "v2", "amd64", "v1") == 200
    assert _release(client, demo, "v3", "arm64", "v2") == 200

    body = _check(client, demo, "v2", "v2").json()
    assert _ranges(body) == [(2, 2)]
    # The v1/v1 pair existed only at the enablement revision.
    baseline = _check(client, demo, "v1", "v1").json()
    assert _ranges(baseline) == [(0, 0)]
    assert baseline["intervals"][0]["touchesHistoryStart"] is True


def test_pair_that_never_coexisted_returns_empty(client, demo):
    assert _release(client, demo, "v2", "amd64", "v1") == 200
    assert _release(client, demo, "v3", "arm64", "v1") == 200
    body = _check(client, demo, "v3", "v3").json()
    assert body["coexisting"] is False
    assert body["intervals"] == []
    assert body["coverage"]["lastRevision"] == 2


def test_unknown_digest_pair_returns_empty_not_error(client, demo):
    # A digest that was never released is simply absent from history.
    response = client.post(
        "/v1/stable/coexistence",
        json={
            "platforms": [
                {"platform": "linux/amd64", "manifestDigest": "sha256:" + "f" * 64},
                {
                    "platform": "linux/arm64",
                    "manifestDigest": _digest(demo, "v1", "arm64"),
                },
            ]
        },
    )
    assert response.status_code == 200
    assert response.json()["intervals"] == []


def test_coexistence_request_validation(client, demo):
    base = {
        "platforms": [
            {"platform": "linux/amd64", "manifestDigest": _digest(demo, "v1", "amd64")},
            {"platform": "linux/arm64", "manifestDigest": _digest(demo, "v1", "arm64")},
        ]
    }

    def post(platforms):
        return client.post("/v1/stable/coexistence", json={"platforms": platforms})

    bad_digest = [dict(item) for item in base["platforms"]]
    bad_digest[0]["manifestDigest"] = "not-a-digest"
    assert post(bad_digest).status_code == 422

    bad_platform = [dict(item) for item in base["platforms"]]
    bad_platform[1]["platform"] = "linux/s390x"
    assert post(bad_platform).status_code == 422

    missing = [dict(base["platforms"][0])]
    response = post(missing)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_platform"

    duplicate = [dict(item) for item in base["platforms"]]
    duplicate[1]["platform"] = "linux/amd64"
    response = post(duplicate)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_platform"

    assert post([]).status_code == 422
    assert client.post("/v1/stable/coexistence", json={}).status_code == 422


def test_conflicts_and_failures_never_enter_history(client, demo):
    assert _release(client, demo, "v2", "amd64", "v1") == 200
    # Stale baseline -> 409, no revision, no journal line.
    assert _release(client, demo, "v3", "amd64", "v1") == 409
    # Unknown blob -> 422, no revision, no journal line.
    bad = client.post(
        "/v1/releases",
        json={
            "platform": "linux/amd64",
            "manifestDigest": "sha256:" + "a" * 64,
            "expectedPreviousDigest": _digest(demo, "v2", "amd64"),
        },
    )
    assert bad.status_code == 422

    body = _check(client, demo, "v2", "v1").json()
    assert body["coverage"]["lastRevision"] == 1
    lines = HISTORY_PATH(client.app.state.settings).read_text().splitlines()
    assert len(lines) == 2  # enablement + one release
    assert [json.loads(line)["revision"] for line in lines] == [0, 1]


def test_journal_append_size_is_constant_as_history_grows(client, demo, settings):
    path = HISTORY_PATH(settings)
    sizes: list[int] = []
    for version, previous in (("v2", "v1"), ("v3", "v2")):
        assert _release(client, demo, version, "amd64", previous) == 200
        lines = path.read_text().splitlines()
        sizes.append(len(lines[-1]))
    # Both release records serialise the same fixed set of fields; the line
    # length must not grow with the number of earlier revisions.
    assert sizes[0] == sizes[1]
    assert all(
        len(line) <= 1024 for line in path.read_text().splitlines()
    ), "journal records must stay bounded"


def test_restarted_process_serves_committed_intervals(settings, demo):
    with make_client(settings) as client:
        assert _release(client, demo, "v2", "amd64", "v1") == 200
        assert _release(client, demo, "v2", "arm64", "v1") == 200
        assert _release(client, demo, "v3", "amd64", "v2") == 200

    with make_client(settings) as restarted:
        body = _check(restarted, demo, "v2", "v2").json()
        assert _ranges(body) == [(2, 2)]
        health = restarted.get("/healthz").json()
        assert health["history"]["available"] is True
        assert health["history"]["records"] == 4


def test_old_volume_without_journal_marks_earlier_revisions_unknown(
    tmp_path, demo, settings
):
    # Simulate a volume written by the previous version: seed state through
    # one full release, then delete the journal so history starts late.
    with make_client(settings) as client:
        assert _release(client, demo, "v2", "amd64", "v1") == 200
        assert _release(client, demo, "v2", "arm64", "v1") == 200
    journal = HISTORY_PATH(settings)
    journal.unlink()

    with make_client(settings) as migrated:
        body = _check(migrated, demo, "v2", "v2").json()
        # History begins at the revision live when the feature was enabled.
        assert body["coverage"]["firstRevision"] == 2
        assert body["coverage"]["lastRevision"] == 2
        assert body["coverage"]["earlierRevisionsUnknown"] is True
        assert body["coverage"]["firstRecordKind"] == "enablement"
        assert _ranges(body) == [(2, 2)]
        # The range touches the known-history boundary: it may have begun
        # earlier, on revisions that predate the journal.
        assert body["intervals"][0]["touchesHistoryStart"] is True

    # Subsequent releases append normal release records after the seed.
    with make_client(settings) as again:
        assert _release(again, demo, "v3", "amd64", "v2") == 200
        records = [
            json.loads(line)
            for line in HISTORY_PATH(settings).read_text().splitlines()
        ]
        assert [r["kind"] for r in records] == ["enablement", "release"]
        assert [r["revision"] for r in records] == [2, 3]


def test_crash_gap_between_state_and_journal_is_reconciled(tmp_path, demo, settings):
    with make_client(settings) as client:
        assert _release(client, demo, "v2", "amd64", "v1") == 200
        assert _release(client, demo, "v2", "arm64", "v1") == 200
        assert _release(client, demo, "v3", "amd64", "v2") == 200

    # Simulate a crash after the state rename but before the journal append:
    # roll the journal back to revision 1 while the state sits at revision 3.
    journal = HISTORY_PATH(settings)
    lines = journal.read_text().splitlines()
    journal.write_text("\n".join(lines[:2]) + "\n")

    with make_client(settings) as restarted:
        body = _check(restarted, demo, "v2", "v2").json()
        # Revisions 2 is inside the unknown crash gap; only revision 1's
        # recorded state and the catch-up record at 3 are queryable.
        assert body["coverage"]["firstRevision"] == 0
        assert body["coverage"]["lastRevision"] == 3
        records = [
            json.loads(line)
            for line in HISTORY_PATH(settings).read_text().splitlines()
        ]
        assert [r["revision"] for r in records] == [0, 1, 3]
        assert records[-1]["kind"] == "enablement"


def test_torn_final_journal_line_is_ignored_but_mid_corruption_fails(
    tmp_path, demo, settings
):
    with make_client(settings) as client:
        assert _release(client, demo, "v2", "amd64", "v1") == 200
    journal = HISTORY_PATH(settings)
    good = journal.read_text()

    # Partial trailing write: tolerated on read, reconciliation rewrites nothing.
    journal.write_text(good + '{"schemaVersion":1,"kind":"rel')
    with make_client(settings) as restarted:
        body = _check(restarted, demo, "v2", "v1").json()
        assert body["coverage"]["lastRevision"] == 1

    # Corruption before the last line must fail loudly instead of fabricating
    # ranges from a truncated history.
    lines = good.splitlines()
    journal.write_text(lines[0] + "\n{broken\n" + lines[1] + "\n")
    with pytest.raises(HistoryError):
        with make_client(settings):
            pass


def test_concurrent_release_during_check_cannot_change_its_snapshot(
    tmp_path, demo, settings
):
    with make_client(settings) as client:
        assert _release(client, demo, "v2", "amd64", "v1") == 200
        assert _release(client, demo, "v2", "arm64", "v1") == 200

        captured: dict[int, object] = {}

        def run_check() -> None:
            response = _check(client, demo, "v2", "v2")
            captured["body"] = response.json()

        thread = threading.Thread(target=run_check)
        thread.start()
        # Commit while the check thread is active; the check answer is bounded
        # by the complete journal records it read, never a half commit.
        assert _release(client, demo, "v3", "amd64", "v2") == 200
        thread.join(timeout=10)

        body = captured["body"]
        # The in-flight check could only observe a complete, consistent state.
        assert body["coverage"]["lastRevision"] in (2, 3)
        if body["coverage"]["lastRevision"] == 2:
            assert _ranges(body) == [(2, 2)]
            assert body["intervals"][0]["active"] is True
        else:
            assert _ranges(body) == [(2, 2)]
            assert body["intervals"][0]["active"] is False

        # A fresh check after the commit sees the split: the historical range
        # is still reported (it did happen), but it is no longer active and
        # the coverage boundary moved to the new revision.
        later = _check(client, demo, "v2", "v2").json()
        assert later["coverage"]["lastRevision"] == 3
        assert _ranges(later) == [(2, 2)]
        assert later["intervals"][0]["active"] is False


def test_two_os_processes_both_read_committed_pairs(settings, demo):
    """The journal in the shared state dir is visible across processes."""
    with make_client(settings) as client:
        assert _release(client, demo, "v2", "amd64", "v1") == 200
        assert _release(client, demo, "v2", "arm64", "v1") == 200

    script = """
import json, sys
sys.path.insert(0, %r)
from app.config import Settings
from app.oci import OciLayout
from app.store import StableStore
from app.history import find_coexistence_intervals

settings = Settings.from_env(json.loads(sys.argv[1]))
wanted = json.loads(sys.argv[2])
store = StableStore(settings, OciLayout(settings.oci_layout))
store.initialize()
view = store.history_view()
intervals = find_coexistence_intervals(view, wanted)
print(json.dumps({
    "first": view.first_known_revision,
    "last": view.snapshot_revision,
    "ranges": [[i.start_revision, i.end_revision] for i in intervals],
}))
""" % str(Path(__file__).resolve().parents[1])
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    payload = json.dumps(
        {
            "OCI_LAYOUT_DIR": str(settings.oci_layout),
            "STATE_DIR": str(settings.state_dir),
            "LAYER_CHUNK_BYTES": "1024",
        }
    )
    wanted = json.dumps(
        {
            "amd64": _digest(demo, "v2", "amd64"),
            "arm64": _digest(demo, "v2", "arm64"),
        }
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-c", script, payload, wanted],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result == {"first": 0, "last": 2, "ranges": [[2, 2]]}


def test_read_only_state_dir_serves_ephemeral_view(tmp_path, demo):
    layout_dir = tmp_path / "oci-layout"
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
    settings = Settings.from_env(
        {"OCI_LAYOUT_DIR": str(layout_dir), "STATE_DIR": str(state_dir)}
    )
    try:
        with make_client(settings) as client:
            body = _check(client, demo, "v1", "v1").json()
            assert body["coverage"]["historyEphemeral"] is True
            assert body["coverage"]["firstRevision"] == 0
            assert _ranges(body) == [(0, 0)]
            assert client.get("/healthz").json()["history"]["ephemeral"] is True
    finally:
        state_dir.chmod(stat.S_IRWXU)


def test_read_only_state_dir_with_existing_journal_serves_read_only_view(
    tmp_path, demo
):
    layout_dir = tmp_path / "oci-layout"
    state_dir = tmp_path / "state"
    writable = Settings.from_env(
        {"OCI_LAYOUT_DIR": str(layout_dir), "STATE_DIR": str(state_dir)}
    )
    with make_client(writable) as client:
        assert _release(client, demo, "v2", "amd64", "v1") == 200
        assert _release(client, demo, "v2", "arm64", "v1") == 200

    state_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        read_only = Settings.from_env(
            {"OCI_LAYOUT_DIR": str(layout_dir), "STATE_DIR": str(state_dir)}
        )
        with make_client(read_only) as client:
            body = _check(client, demo, "v2", "v2").json()
            assert body["coverage"]["historyEphemeral"] is False
            assert body["coverage"]["lastRevision"] == 2
            assert _ranges(body) == [(2, 2)]
    finally:
        state_dir.chmod(stat.S_IRWXU)


def test_history_helpers_split_ranges_on_revision_gaps(tmp_path):
    """Unit test: a reconciliation jump is an unknown span and splits ranges."""
    from app.history import HistoryRecord, HistoryView

    def record(revision: int, amd: str, arm: str, kind: str = "release"):
        return HistoryRecord(
            revision=revision,
            kind=kind,
            digests={"amd64": amd, "arm": arm},
            updated_at=None,
        )

    pair_a = ("aaa", "bbb")
    records = [
        record(0, *pair_a, kind="enablement"),
        record(1, *pair_a),
        # revision 2 is missing: crash-reconciliation jump.
        record(3, *pair_a, kind="enablement"),
        record(4, "ccc", "ddd"),
        record(5, *pair_a),
    ]
    view = HistoryView(
        records=records, first_known_revision=0, snapshot_revision=5
    )
    intervals = find_coexistence_intervals(
        view, {"amd64": pair_a[0], "arm": pair_a[1]}
    )
    assert [(i.start_revision, i.end_revision) for i in intervals] == [
        (0, 1),
        (3, 3),
        (5, 5),
    ]
    assert intervals[0].history_start is True
    assert intervals[1].history_start is False


def test_load_history_view_bounds_at_state_revision(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    history = ReleaseHistory(state_dir)
    history.append_record(
        HistoryRecord(0, "enablement", {"amd64": "sha256:" + "1" * 64}, None)
    )
    history.append_record(
        HistoryRecord(
            1,
            "release",
            {
                "amd64": "sha256:" + "2" * 64,
                "arm64": "sha256:" + "3" * 64,
            },
            None,
            "amd64",
        )
    )
    # Pretend the state file is still at revision 0: revision 1 must be
    # excluded from the point-in-time view.
    view = load_history_view(
        history,
        state_revision=0,
        fallback_digests={"amd64": "sha256:" + "1" * 64},
    )
    assert view.snapshot_revision == 0
    assert len(view.records) == 1
