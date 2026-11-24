# Edge stable release API

A FastAPI service that releases **one architecture at a time** into a
dual-architecture (linux/amd64 + linux/arm64) stable OCI index. Publishing an
amd64 fix never requires re-pushing arm64, and vice versa.

Image content is verified from a **local, read-only OCI image layout** — no
registry access, no network fetches.

## Architecture

```
                 read-only mount                 writable volume
┌──────────────┐   ┌────────────────────┐   ┌──────────────────────┐
│ layout-init  │──▶│ /oci-layout        │   │ /state               │
│ (one-shot)   │   │  oci-layout        │   │  stable-index.json   │
└──────────────┘   │  index.json        │   │  stable-index.lock   │
                   │  blobs/sha256/…    │   └──────────┬───────────┘
                   └────────┬───────────┘              │
                            │ verify (chunked hashing) │ CAS commit
                   ┌────────▼───────────────────────────▼──────────┐
                   │ release-api (uvicorn, 2 workers)              │
                   │ GET  /v1/stable   POST /v1/releases           │
                   └───────────────────────────────────────────────┘
```

### Request flow for `POST /v1/releases`

1. Validate `platform` is `linux/amd64` or `linux/arm64` and both digests are
   well-formed `sha256:…`.
2. **Outside any lock**, verify the candidate manifest:
   - manifest blob exists and its bytes hash to the submitted digest,
     `schemaVersion` is 2, media type allowed;
   - config descriptor: media type, size, digest, and the config's own
     `os` / `architecture` must equal `linux` / the requested architecture;
   - every layer descriptor: media type, `size` (checked against the blob by
     `stat`, again while streaming, and on the final byte count) and digest,
     hashed in bounded chunks (`LAYER_CHUNK_BYTES`).
3. **Under an `flock` + compare-and-swap**, re-read the persisted index; if the
   target platform's digest differs from `expectedPreviousDigest` → `409` with
   the current digest, writing nothing. Otherwise replace only that platform's
   descriptor, keep the other exactly as it is, bump the revision, and persist.
4. Persist atomically (temp file → `fsync` → `rename` → directory `fsync`), so a
   reader or a crash only ever sees the whole old or whole new index.

Consequences:

* Layer scanning never holds the commit lock, so `GET /v1/stable` stays
  responsive and the other architecture can commit mid-scan.
* Failed verification (422) or a conflict (409) leaves the persisted index
  untouched; after a restart the last successful index is served.
* Two uvicorn workers (or two containers) sharing `/state` serialise commits
  through the file lock and each read the latest committed state.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/v1/stable` | Full OCI index (`schemaVersion`, `mediaType`, both `manifests`). `ETag`/`304`. |
| `GET` | `/v1/stable/{arch}` | One platform descriptor (`amd64` or `arm64`). |
| `POST` | `/v1/releases` | Release one architecture. |
| `GET` | `/v1/releases` | Per-architecture last publication + currently active scans. |
| `GET` | `/healthz` | Readiness, revision, persistence status, in-flight scans. |
| `GET` | `/docs` | OpenAPI UI. |

## Run with Docker Compose

```console
docker compose up -d --build
curl -s localhost:8000/v1/stable | jq .
docker compose exec release-api python /srv/examples/demo_concurrent.py
```

`docker-compose.yml`:

* `layout-init` builds a deterministic demo layout into the `oci-layout` named
  volume (preloaded stable **v1** on both architectures, plus **v2** and **v3**
  candidate manifests as blobs) and writes `demo-digests.json` beside it.
* `release-api` mounts `oci-layout` **read-only** and `release-state`
  read-write. It runs two uvicorn workers to exercise the cross-process commit
  path. `LAYER_CHUNK_DELAY_MS=1` is a demo-only pause per 64 KiB chunk so the
  concurrency is visible on small demo layers; set it to `0` in production.

Reset the demo (returns to the v1 baseline):

```console
docker compose down -v && docker compose up -d
```

## Local run without Docker

Requires Python 3.11+.

```console
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt

python scripts/build_layout.py --out /tmp/oci-layout --meta /tmp/oci-layout/demo-digests.json

OCI_LAYOUT_DIR=/tmp/oci-layout STATE_DIR=/tmp/state \
  uvicorn app.main:create_app --factory --port 8000 --workers 2

# in another shell
python examples/demo_concurrent.py --meta /tmp/oci-layout/demo-digests.json
```

Tests (54 cases: validation, conflicts, durability, in-process and cross-process concurrency, endpoints):

```console
python -m pytest -q
```

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `OCI_LAYOUT_DIR` | `/oci-layout` | OCI image layout root (read-only). |
| `STATE_DIR` | `/state` | Writable directory for `stable-index.json`. |
| `OCI_INDEX_REF` | `registry.example.com/edge/stable:latest` | Reference recorded on the index. |
| `OCI_ARCHITECTURES` | `amd64,arm64` | Platforms the index must contain. |
| `LAYER_CHUNK_BYTES` | `65536` | Blob hashing chunk size (1 B – 16 MiB). |
| `LAYER_CHUNK_DELAY_MS` | `0` | Demo-only per-chunk pause (0–1000 ms). |

## Input layout expectations

`<layout>/oci-layout` must contain `imageLayoutVersion: 1.0.0`.
`<layout>/index.json` may either list platform descriptors directly, or hold a
single descriptor whose `mediaType` is an OCI/Docker **index** and whose blob is
the nested multi-arch index (the demo does the latter). The resolved index must:

* have `schemaVersion: 2` and an OCI/Docker index media type,
* contain exactly one `linux/<arch>` descriptor per configured architecture,
* reference manifest blobs that exist and whose configs declare the matching
  platform.

Anything else fails startup with `invalid_layout` rather than serving an index
the service cannot honour.

## What was exercised

The test suite, a live two-worker uvicorn process, and the concurrent demo
script were run against generated layouts in this environment. Docker itself is
not installed here, so `docker compose up` was not executed; the Compose file is
validated by YAML parsing only. See the delivery notes in the conversation.
