# HTTP walkthrough

All commands assume the Compose stack is up (`docker compose up -d`).

## 1. Query the full stable index

```console
$ curl -s localhost:8000/v1/stable | jq .
{
  "schemaVersion": 2,
  "mediaType": "application/vnd.oci.image.index.v1+json",
  "manifests": [
    {
      "mediaType": "application/vnd.oci.image.manifest.v1+json",
      "digest": "sha256:798460f5…",
      "size": 664,
      "platform": {"architecture": "amd64", "os": "linux"}
    },
    {
      "mediaType": "application/vnd.oci.image.manifest.v1+json",
      "digest": "sha256:adb94625…",
      "size": 664,
      "platform": {"architecture": "arm64", "os": "linux", "variant": "v8"}
    }
  ],
  "annotations": {"org.opencontainers.image.ref.name": "registry.example.com/edge/stable:latest"}
}
```

The response is always the complete two-architecture index, with
`ETag`, `X-Index-Revision` and `X-Index-Updated-At` headers. Send
`If-None-Match` to get `304` when nothing changed.

## 2. Pick target and baseline digests from the demo metadata

The layout volume contains `demo-digests.json`, written by the layout-init job:

```console
$ docker compose exec release-api \
    jq -r '.versions | to_entries[] | "\(.key) \(.value.amd64.digest)"' \
    /oci-layout/demo-digests.json
v1 sha256:798460f5…
v2 sha256:de954dbc…
v3 sha256:6c810cf9…
```

`v1` is stable on both architectures at startup. `v2`/`v3` are the candidates.

## 3. Release only amd64

```console
$ curl -s -X POST localhost:8000/v1/releases \
    -H 'content-type: application/json' \
    -d '{"platform":"linux/amd64",
         "manifestDigest":"sha256:de954dbc…",
         "expectedPreviousDigest":"sha256:798460f5…"}' | jq .
{
  "ref": "registry.example.com/edge/stable:latest",
  "revision": 1,
  "updatedAt": "2026-11-24T10:30:00.000Z",
  "released": {
    "platform": "linux/amd64",
    "previousDigest": "sha256:798460f5…",
    "digest": "sha256:de954dbc…",
    "descriptor": { "...": "index descriptor for amd64" },
    "verified": {"configBytes": 431, "layers": 2, "layerBytes": 4194304, "chunks": 66, "elapsedMs": 12.4}
  },
  "stable": { "schemaVersion": 2, "manifests": ["…amd64 v2…", "…arm64 v1 unchanged…"] }
}
```

The arm64 descriptor is exactly what it was at commit time.

## 4. Release arm64 in parallel

```console
$ curl -s -X POST localhost:8000/v1/releases \
    -H 'content-type: application/json' \
    -d '{"platform":"linux/arm64",
         "manifestDigest":"sha256:cb165f4b…",
         "expectedPreviousDigest":"sha256:adb94625…"}' | jq .revision
2
```

While a layer scan is running, `GET /v1/stable` keeps returning the previous
digest for the in-flight architecture and the other architecture can commit.
`examples/demo_concurrent.py` drives exactly this scenario.

## 5. Conflict on a stale baseline

Re-submitting the amd64 v1 baseline after it has moved to v2:

```console
$ curl -s -o /tmp/out.json -w '%{http_code}\n' -X POST localhost:8000/v1/releases \
    -H 'content-type: application/json' \
    -d '{"platform":"linux/amd64",
         "manifestDigest":"sha256:6c810cf9…",
         "expectedPreviousDigest":"sha256:798460f5…"}'
409

$ jq .error /tmp/out.json
{
  "code": "conflict",
  "message": "linux/amd64 baseline changed: expected sha256:798460f5…, current sha256:de954dbc…",
  "details": {
    "platform": "linux/amd64",
    "expectedPreviousDigest": "sha256:798460f5…",
    "currentDigest": "sha256:de954dbc…"
  }
}
```

The index is untouched. Retry with the current digest to succeed.

## 6. Error responses worth knowing

| Status | `error.code` | Meaning |
| --- | --- | --- |
| 422 | `invalid_platform` | not `linux/amd64` or `linux/arm64` |
| 422 | `platform_mismatch` | manifest config says a different architecture |
| 422 | `blob_not_found` | manifest/config/layer absent from the read-only layout |
| 422 | `digest_mismatch` | manifest or layer content does not hash to the descriptor |
| 422 | `size_mismatch` | descriptor `size` differs from the actual blob length |
| 422 | `unsupported_media_type` | manifest/config/layer media type not allowed |
| 409 | `conflict` | `expectedPreviousDigest` no longer matches the stable index |
| 503 | `state_unavailable` | durable state is unreadable/unwritable |

## 7. Restart behaviour

```console
$ docker compose restart release-api
$ curl -s localhost:8000/v1/stable | jq '.manifests[].digest'
"sha256:de954dbc…"
"sha256:cb165f4b…"
```

The last successful release is served from `/state/stable-index.json`; a failed
release never overwrites it.
