"""FastAPI release API for a dual-architecture stable OCI index."""

from __future__ import annotations

import logging
import threading
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import __version__
from .config import Settings
from .oci import (
    OCI_INDEX_MEDIA_TYPE,
    OciLayout,
    ReleaseValidationError,
    validate_digest,
    verify_release,
)
from .store import ConflictError, StableStore, StoreError

logger = logging.getLogger("release-api")


class ReleaseRequest(BaseModel):
    platform: str = Field(
        ...,
        description="Target platform, exactly 'linux/amd64' or 'linux/arm64'.",
        examples=["linux/amd64"],
    )
    manifestDigest: str = Field(
        ...,
        description="Digest of the new single-platform image manifest.",
        examples=["sha256:" + "0" * 64],
    )
    expectedPreviousDigest: str = Field(
        ...,
        description="Digest the caller believes is currently stable for that platform.",
        examples=["sha256:" + "1" * 64],
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "platform": "linux/amd64",
                    "manifestDigest": "sha256:" + "a" * 64,
                    "expectedPreviousDigest": "sha256:" + "b" * 64,
                }
            ]
        }
    }


class ActiveRelease:
    __slots__ = ("architecture", "started", "manifest_digest")

    def __init__(self, architecture: str, manifest_digest: str) -> None:
        self.architecture = architecture
        self.started = time.monotonic()
        self.manifest_digest = manifest_digest


class ReleaseService:
    def __init__(self, settings: Settings, layout: OciLayout, store: StableStore) -> None:
        self.settings = settings
        self.layout = layout
        self.store = store
        self._active: dict[int, ActiveRelease] = {}
        self._active_lock = threading.Lock()
        self._next_token = 0

    def begin(self, architecture: str, manifest_digest: str) -> int:
        with self._active_lock:
            self._next_token += 1
            token = self._next_token
            self._active[token] = ActiveRelease(architecture, manifest_digest)
            return token

    def end(self, token: int) -> None:
        with self._active_lock:
            self._active.pop(token, None)

    def active(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        with self._active_lock:
            return [
                {
                    "platform": f"linux/{item.architecture}",
                    "manifestDigest": item.manifest_digest,
                    "elapsedMs": round((now - item.started) * 1000, 1),
                }
                for item in self._active.values()
            ]


def _error(status: int, code: str, message: str, **details: Any) -> JSONResponse:
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    return JSONResponse(status_code=status, content=body)


def _parse_platform(raw: str, settings: Settings) -> str:
    if not isinstance(raw, str) or raw.count("/") != 1:
        raise ReleaseValidationError(
            "platform must be 'linux/<architecture>'", code="invalid_platform"
        )
    os_name, arch = raw.split("/", 1)
    if os_name != "linux":
        raise ReleaseValidationError(
            f"only linux platforms are supported, got {os_name!r}", code="invalid_platform"
        )
    if arch not in settings.architectures:
        raise ReleaseValidationError(
            f"architecture {arch!r} is not configured; expected one of "
            + ", ".join(settings.architectures),
            code="invalid_platform",
        )
    return arch


def create_app(
    settings: Settings | None = None,
    *,
    layout: OciLayout | None = None,
    store: StableStore | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    layout = layout or OciLayout(settings.oci_layout)
    store = store or StableStore(settings, layout)
    service = ReleaseService(settings, layout, store)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        snapshot = store.initialize()
        logger.info(
            "loaded stable index ref=%s revision=%d persisted=%s platforms=%s",
            settings.index_ref,
            snapshot.revision,
            snapshot.persisted,
            [m.get("platform", {}) for m in snapshot.index.get("manifests", [])],
        )
        yield

    app = FastAPI(
        title="Edge Stable Release API",
        version=__version__,
        description=(
            "Publishes single-architecture image manifests into a dual-architecture "
            "OCI stable index. Verification reads a local, read-only OCI image layout."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.layout = layout
    app.state.store = store
    app.state.service = service

    @app.get("/healthz", summary="Liveness and readiness")
    def healthz() -> dict[str, Any]:
        try:
            snapshot = store.snapshot()
        except StoreError as exc:
            return JSONResponse(
                status_code=503,
                content={"status": "degraded", "reason": str(exc)},
            )
        return {
            "status": "ok",
            "ref": settings.index_ref,
            "revision": snapshot.revision,
            "persisted": snapshot.persisted,
            "architectures": list(settings.architectures),
            "activeReleases": service.active(),
        }

    @app.get(
        "/v1/stable",
        summary="Return the full stable OCI index",
        response_description="The complete two-architecture OCI image index.",
    )
    def get_stable(request: Request) -> Any:
        try:
            snapshot = store.snapshot()
        except StoreError as exc:
            return _error(503, "state_unavailable", str(exc))
        etag = f'"{snapshot.etag}"'
        headers = {
            "ETag": etag,
            "Cache-Control": "no-cache",
            "X-Index-Revision": str(snapshot.revision),
        }
        if snapshot.updated_at:
            headers["X-Index-Updated-At"] = snapshot.updated_at
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        return JSONResponse(
            status_code=200,
            content=snapshot.index,
            headers=headers,
            media_type=OCI_INDEX_MEDIA_TYPE,
        )

    @app.get("/v1/stable/{platform_arch}", summary="Return one platform descriptor")
    def get_stable_platform(platform_arch: str) -> Any:
        try:
            arch = _parse_platform(f"linux/{platform_arch}", settings)
            snapshot = store.snapshot()
        except ReleaseValidationError as exc:
            return _error(404, exc.code, str(exc))
        except StoreError as exc:
            return _error(503, "state_unavailable", str(exc))
        for desc in snapshot.index["manifests"]:
            if desc.get("platform", {}).get("architecture") == arch:
                return JSONResponse(
                    status_code=200,
                    content=desc,
                    headers={"X-Index-Revision": str(snapshot.revision)},
                )
        return _error(404, "platform_not_found", f"no descriptor for linux/{arch}")

    @app.get("/v1/releases", summary="Publication history per architecture")
    def get_releases() -> Any:
        try:
            snapshot, published = store.view()
        except StoreError as exc:
            return _error(503, "state_unavailable", str(exc))
        return {
            "ref": settings.index_ref,
            "revision": snapshot.revision,
            "updatedAt": snapshot.updated_at,
            "published": published,
            "active": service.active(),
        }

    @app.post(
        "/v1/releases",
        summary="Release one architecture into the stable index",
        status_code=200,
    )
    def create_release(payload: ReleaseRequest) -> Any:
        try:
            arch = _parse_platform(payload.platform, settings)
            manifest_digest = validate_digest(
                payload.manifestDigest, what="manifestDigest"
            )
            expected = validate_digest(
                payload.expectedPreviousDigest, what="expectedPreviousDigest"
            )
        except ReleaseValidationError as exc:
            return _error(422, exc.code, str(exc))

        token = service.begin(arch, manifest_digest)
        try:
            # Full verification happens outside the commit lock, so a slow
            # layer scan never blocks GET /v1/stable or the other architecture.
            verified = verify_release(layout, settings, arch, manifest_digest)
            if verified.platform != payload.platform:
                return _error(
                    422,
                    "platform_mismatch",
                    f"verified platform {verified.platform} != requested {payload.platform}",
                )
            snapshot, record = store.commit(verified, expected)
        except ConflictError as exc:
            return _error(
                409,
                "conflict",
                str(exc),
                platform=exc.platform,
                expectedPreviousDigest=exc.expected,
                currentDigest=exc.current,
            )
        except ReleaseValidationError as exc:
            return _error(422, exc.code, str(exc))
        except StoreError as exc:
            return _error(503, "state_unavailable", str(exc))
        finally:
            service.end(token)

        return JSONResponse(
            status_code=200,
            content={
                "ref": settings.index_ref,
                "revision": snapshot.revision,
                "updatedAt": snapshot.updated_at,
                "released": {
                    "platform": verified.platform,
                    "previousDigest": record["previousDigest"],
                    "digest": verified.manifest_digest,
                    "descriptor": verified.descriptor,
                    "verified": {
                        "configBytes": verified.config_bytes,
                        "layers": verified.layer_count,
                        "layerBytes": verified.layer_bytes,
                        "chunks": verified.chunks,
                        "elapsedMs": verified.elapsed_ms,
                    },
                },
                "stable": snapshot.index,
            },
            headers={
                "ETag": f'"{snapshot.etag}"',
                "X-Index-Revision": str(snapshot.revision),
            },
        )

    @app.exception_handler(RequestValidationError)
    async def _request_validation(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _error(
            422,
            "invalid_request",
            "request body failed validation",
            errors=list(exc.errors()),
        )

    return app


# Constructed by uvicorn's factory mode (``app.main:create_app --factory``) so
# importing this module never requires a mounted layout, which keeps tests and
# one-off tooling importable.
