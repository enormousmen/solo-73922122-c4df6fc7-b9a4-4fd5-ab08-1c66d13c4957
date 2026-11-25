"""Append-only history of stable-index revisions.

Every successfully committed release appends one fixed-shape record to
``release-history.jsonl``.  Because each append is a single line of bounded
size (one digest per configured architecture), the write cost of a release is
constant and does not grow with the length of the history.

Durability model
----------------
* The JSONL file is opened with ``O_APPEND`` while the cross-process commit
  lock (see :class:`app.store.StableStore`) is held, so commits from different
  worker processes serialise and lines can never interleave.
* A reader never takes the lock.  A torn final line (crash mid-write) is
  ignored, while corruption anywhere earlier is a hard error: silently
  truncating the middle of the history would fabricate version ranges.
* The journal may legitimately lag the state file when a crash happens
  between the atomic state rename and the journal append.  Startup
  reconciliation appends a catch-up record at the state revision, so the gap
  becomes one explicitly unknown span rather than a missing file.
* A state directory created by an older release (state file, no journal) is
  treated the same way: history starts at the revision that was live when the
  feature was enabled, and every earlier revision stays unknown.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .oci import validate_digest

HISTORY_FILENAME = "release-history.jsonl"
HISTORY_SCHEMA_VERSION = 1
RECORD_KIND_ENABLEMENT = "enablement"
RECORD_KIND_RELEASE = "release"


class HistoryError(Exception):
    """The history journal is missing, unreadable, or structurally invalid."""


@dataclass(frozen=True)
class HistoryRecord:
    """One complete entry of the append-only journal."""

    revision: int
    kind: str
    # Architecture -> manifest digest active at this revision.
    digests: dict[str, str]
    updated_at: str | None
    # Architecture that changed in this commit; None for enablement records.
    released_architecture: str | None = None


@dataclass(frozen=True)
class HistoryView:
    """Everything a point-in-time coexistence check is allowed to see."""

    records: list[HistoryRecord]
    first_known_revision: int
    snapshot_revision: int
    # True when no durable journal exists and the view was synthesised from
    # the in-memory fallback index (read-only state directory deployments).
    ephemeral: bool = False


def _parse_record(raw_line: str, *, line_number: int) -> HistoryRecord:
    try:
        record = json.loads(raw_line)
    except json.JSONDecodeError as exc:
        raise HistoryError(
            f"history journal line {line_number} is not valid JSON: {exc.msg}"
        ) from exc
    if not isinstance(record, dict):
        raise HistoryError(f"history journal line {line_number} must be a JSON object")
    if record.get("schemaVersion") != HISTORY_SCHEMA_VERSION:
        raise HistoryError(
            f"history journal line {line_number} has unsupported schemaVersion"
        )
    kind = record.get("kind")
    if kind not in (RECORD_KIND_ENABLEMENT, RECORD_KIND_RELEASE):
        raise HistoryError(f"history journal line {line_number} has unknown kind {kind!r}")
    revision = record.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise HistoryError(
            f"history journal line {line_number} has an invalid revision"
        )
    raw_digests = record.get("digests")
    if not isinstance(raw_digests, dict) or not raw_digests:
        raise HistoryError(
            f"history journal line {line_number} must map architectures to digests"
        )
    digests: dict[str, str] = {}
    for architecture, digest in raw_digests.items():
        if not isinstance(architecture, str) or not architecture:
            raise HistoryError(
                f"history journal line {line_number} has an invalid architecture key"
            )
        digests[architecture] = validate_digest(
            digest, what=f"history journal line {line_number} {architecture} digest"
        )
    updated_at = record.get("updatedAt")
    if updated_at is not None and not isinstance(updated_at, str):
        raise HistoryError(
            f"history journal line {line_number} has an invalid updatedAt"
        )
    released_architecture = record.get("releasedArchitecture")
    if released_architecture is not None and not isinstance(released_architecture, str):
        raise HistoryError(
            f"history journal line {line_number} has an invalid releasedArchitecture"
        )
    return HistoryRecord(
        revision=revision,
        kind=kind,
        digests=digests,
        updated_at=updated_at,
        released_architecture=released_architecture,
    )


def _record_to_json(record: HistoryRecord) -> bytes:
    payload: dict[str, Any] = {
        "schemaVersion": HISTORY_SCHEMA_VERSION,
        "kind": record.kind,
        "revision": record.revision,
        "digests": dict(sorted(record.digests.items())),
        "updatedAt": record.updated_at,
    }
    if record.released_architecture is not None:
        payload["releasedArchitecture"] = record.released_architecture
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


class ReleaseHistory:
    """Manages the append-only revision journal inside the state directory."""

    def __init__(self, state_dir: Path) -> None:
        self.path = Path(state_dir) / HISTORY_FILENAME

    def exists(self) -> bool:
        return self.path.is_file()

    def append_record(self, record: HistoryRecord) -> None:
        """Append one record durably. Callers must hold the commit lock."""
        payload = _record_to_json(record)
        try:
            handle = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        except OSError as exc:
            raise HistoryError(f"cannot open history journal {self.path}: {exc}") from exc
        try:
            written = os.write(handle, payload)
            if written != len(payload):
                raise HistoryError("short write while appending to history journal")
            os.fsync(handle)
        except OSError as exc:
            raise HistoryError(f"cannot append to history journal {self.path}: {exc}") from exc
        finally:
            os.close(handle)

    def read_records(self) -> list[HistoryRecord]:
        """Read every complete record, tolerating only a torn final line."""
        try:
            raw = self.path.read_bytes()
        except OSError as exc:
            raise HistoryError(f"cannot read history journal {self.path}: {exc}") from exc
        if not raw:
            raise HistoryError(f"history journal {self.path} exists but is empty")
        text = raw.decode("utf-8")
        lines = text.split("\n")
        # split() always yields one trailing element; it is empty when the file
        # ends with the canonical newline.
        if lines and lines[-1] == "":
            lines.pop()
        records: list[HistoryRecord] = []
        for position, line in enumerate(lines, start=1):
            if not line:
                raise HistoryError(
                    f"history journal line {position} is empty (embedded blank line)"
                )
            # A crash between os.write() and completion can leave a partial
            # final line. Only that last, unterminated/garbled line is skippable;
            # corruption anywhere earlier must fail loudly.
            try:
                record = _parse_record(line, line_number=position)
            except HistoryError:
                if position == len(lines):
                    break
                raise
            if records and record.revision <= records[-1].revision:
                raise HistoryError(
                    "history journal revisions must strictly increase: "
                    f"{records[-1].revision} -> {record.revision}"
                )
            records.append(record)
        if not records:
            raise HistoryError(f"history journal {self.path} holds no valid records")
        return records


def reconcile_history(
    history: ReleaseHistory,
    *,
    state_revision: int,
    state_digests: dict[str, str],
    state_updated_at: str | None,
) -> None:
    """Make the journal consistent with an already-persisted state file.

    Runs under the commit lock at startup. Three cases:

    * no journal yet (old volume, or first boot on a fresh state dir) ->
      seed an enablement record at the current state revision;
    * journal behind the state (crash between state rename and journal
      append, or several missed commits) -> one catch-up enablement record,
      leaving the missed span explicitly unknown;
    * journal in sync -> nothing. A journal ahead of the state is an error.
    """
    if not history.exists():
        history.append_record(
            HistoryRecord(
                revision=state_revision,
                kind=RECORD_KIND_ENABLEMENT,
                digests=dict(state_digests),
                updated_at=state_updated_at,
            )
        )
        return
    records = history.read_records()
    last_journal_revision = records[-1].revision
    if last_journal_revision == state_revision:
        return
    if last_journal_revision > state_revision:
        raise HistoryError(
            "history journal is ahead of the state file: journal revision "
            f"{last_journal_revision}, state revision {state_revision}"
        )
    history.append_record(
        HistoryRecord(
            revision=state_revision,
            kind=RECORD_KIND_ENABLEMENT,
            digests=dict(state_digests),
            updated_at=state_updated_at,
        )
    )


def load_history_view(
    history: ReleaseHistory,
    *,
    state_revision: int,
    fallback_digests: dict[str, str],
) -> HistoryView:
    """Point-in-time view bounded by the last fully committed revision.

    Reads take no lock. The view ends at the last complete journal record,
    which is always <= the state revision, so a check never observes a
    half-published commit while releases keep flowing.
    """
    if not history.exists():
        # Read-only state directory: nothing was ever persisted durably.
        return HistoryView(
            records=[
                HistoryRecord(
                    revision=state_revision,
                    kind=RECORD_KIND_ENABLEMENT,
                    digests=dict(fallback_digests),
                    updated_at=None,
                )
            ],
            first_known_revision=state_revision,
            snapshot_revision=state_revision,
            ephemeral=True,
        )
    records = history.read_records()
    snapshot_revision = min(records[-1].revision, state_revision)
    bounded = [record for record in records if record.revision <= snapshot_revision]
    if not bounded:
        raise HistoryError("history journal has no record at or below the state revision")
    return HistoryView(
        records=bounded,
        first_known_revision=bounded[0].revision,
        snapshot_revision=snapshot_revision,
    )


@dataclass(frozen=True)
class CoexistenceInterval:
    start_revision: int
    end_revision: int
    start_kind: str
    end_kind: str
    history_start: bool


def _record_at(view: HistoryView, revision: int) -> HistoryRecord:
    for record in view.records:
        if record.revision == revision:
            return record
    raise HistoryError(f"internal: no history record at revision {revision}")


def _close_interval(
    intervals: list[CoexistenceInterval],
    view: HistoryView,
    start_revision: int,
    end_revision: int,
) -> None:
    start_record = _record_at(view, start_revision)
    end_record = _record_at(view, end_revision)
    intervals.append(
        CoexistenceInterval(
            start_revision=start_revision,
            end_revision=end_revision,
            start_kind=start_record.kind,
            end_kind=end_record.kind,
            history_start=start_revision == view.first_known_revision,
        )
    )


def find_coexistence_intervals(
    view: HistoryView, wanted: dict[str, str]
) -> list[CoexistenceInterval]:
    """Return maximal contiguous revision ranges matching ``wanted``.

    ``wanted`` maps each architecture to the digest the caller asks about.
    A revision matches only when every architecture is on its requested
    digest simultaneously. Splits end the current range; the same pair
    reappearing later opens a new one, so two ranges are listed separately.
    A revision gap (for example a crash-reconciliation jump) also splits a
    range, because nothing is known about the revisions inside the gap.
    """
    intervals: list[CoexistenceInterval] = []
    current_start: int | None = None
    previous_revision: int | None = None

    for record in view.records:
        matches = all(
            record.digests.get(architecture) == digest
            for architecture, digest in wanted.items()
        )
        contiguous = (
            previous_revision is None or record.revision == previous_revision + 1
        )
        if matches:
            if current_start is None:
                current_start = record.revision
            elif not contiguous:
                # Unknown span in between: close the old range before reopening.
                _close_interval(intervals, view, current_start, previous_revision)  # type: ignore[arg-type]
                current_start = record.revision
        elif current_start is not None:
            _close_interval(intervals, view, current_start, previous_revision)  # type: ignore[arg-type]
            current_start = None
        previous_revision = record.revision

    if current_start is not None:
        _close_interval(intervals, view, current_start, previous_revision)  # type: ignore[arg-type]
    return intervals
