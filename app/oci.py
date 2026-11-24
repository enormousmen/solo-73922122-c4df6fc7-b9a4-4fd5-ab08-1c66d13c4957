"""OCI image layout verification.

The layout is opened strictly read-only.  Everything the release API needs to
verify a submitted manifest (manifest, config, layers) must already be present
in ``blobs/sha256``.  No remote registry access happens here.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Settings

INDEX_MEDIA_TYPES = frozenset(
    {
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    }
)
OCI_INDEX_MEDIA_TYPE = "application/vnd.oci.image.index.v1+json"
CONFIG_MEDIA_TYPES = frozenset(
    {
        "application/vnd.oci.image.config.v1+json",
        "application/vnd.docker.container.image.v1+json",
    }
)
LAYER_MEDIA_TYPES = frozenset(
    {
        "application/vnd.oci.image.layer.v1.tar",
        "application/vnd.oci.image.layer.v1.tar+gzip",
        "application/vnd.oci.image.layer.v1.tar+zstd",
        "application/vnd.docker.image.rootfs.diff.tar",
        "application/vnd.docker.image.rootfs.diff.tar.gzip",
        "application/vnd.docker.image.rootfs.diff.tar.zstd",
        "application/vnd.oci.image.layer.nondistributable.v1.tar",
        "application/vnd.oci.image.layer.nondistributable.v1.tar+gzip",
        "application/vnd.oci.image.layer.nondistributable.v1.tar+zstd",
        "application/vnd.docker.image.rootfs.foreign.diff.tar.gzip",
    }
)

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_CONFIG_BYTES = 4 * 1024 * 1024


class OciError(Exception):
    """Base class for errors raised while reading or verifying an image."""

    code = "oci_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class LayoutError(OciError):
    """The OCI layout on disk is missing or malformed."""

    code = "invalid_layout"


class ReleaseValidationError(OciError):
    """The submitted release failed verification (HTTP 422)."""

    code = "invalid_release"


def validate_digest(value: Any, *, what: str = "digest") -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise ReleaseValidationError(
            f"{what} must be a sha256:<64 lowercase hex> digest", code="invalid_digest"
        )
    return value


def digest_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class HashStats:
    bytes_hashed: int
    chunks: int


@dataclass(frozen=True)
class VerifiedRelease:
    """Result of full manifest/config/layer verification."""

    platform: str
    architecture: str
    os: str
    manifest_digest: str
    descriptor: dict[str, Any]
    config_bytes: int
    layer_count: int
    layer_bytes: int
    chunks: int
    elapsed_ms: float


@dataclass
class OciLayout:
    """Read-only access to an OCI image layout."""

    root: Path
    _blob_root: Path = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        marker = self.root / "oci-layout"
        if not marker.is_file():
            raise LayoutError(f"missing oci-layout marker at {marker}")
        try:
            marker_data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LayoutError(f"cannot read oci-layout marker: {exc}") from exc
        if not isinstance(marker_data, dict) or marker_data.get("imageLayoutVersion") != "1.0.0":
            raise LayoutError("unsupported or missing imageLayoutVersion in oci-layout")
        if not (self.root / "index.json").is_file():
            raise LayoutError(f"missing index.json at {self.root / 'index.json'}")
        self._blob_root = self.root / "blobs" / "sha256"
        if not self._blob_root.is_dir():
            raise LayoutError(f"missing blob directory at {self._blob_root}")

    def blob_path(self, digest: str) -> Path:
        validate_digest(digest)
        return self._blob_root / digest.split(":", 1)[1]

    def blob_exists(self, digest: str) -> bool:
        return self.blob_path(digest).is_file()

    def read_blob(self, digest: str, *, max_bytes: int, what: str) -> bytes:
        path = self.blob_path(digest)
        try:
            size = path.stat().st_size
        except FileNotFoundError as exc:
            raise ReleaseValidationError(
                f"{what} blob not found in local layout: {digest}", code="blob_not_found"
            ) from exc
        if size > max_bytes:
            raise ReleaseValidationError(
                f"{what} blob {digest} is {size} bytes, exceeding limit {max_bytes}",
                code="blob_too_large",
            )
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ReleaseValidationError(
                f"cannot read {what} blob {digest}: {exc}", code="blob_unreadable"
            ) from exc
        if len(data) != size:
            raise ReleaseValidationError(
                f"{what} blob {digest} changed size while reading", code="blob_unreadable"
            )
        return data

    def hash_blob(
        self,
        digest: str,
        *,
        expected_size: int,
        chunk_size: int,
        chunk_delay: float,
    ) -> HashStats:
        """Hash a blob in bounded chunks, checking its declared size.

        Size is checked first via ``stat`` to fail fast on obvious mismatches,
        then again while streaming so a truncated or growing file cannot slip
        through.
        """
        path = self.blob_path(digest)
        try:
            actual_size = path.stat().st_size
        except FileNotFoundError as exc:
            raise ReleaseValidationError(
                f"layer blob not found in local layout: {digest}", code="blob_not_found"
            ) from exc
        if actual_size != expected_size:
            raise ReleaseValidationError(
                f"layer {digest} descriptor size {expected_size} does not match blob size {actual_size}",
                code="size_mismatch",
            )

        hasher = hashlib.sha256()
        total = 0
        chunks = 0
        try:
            with path.open("rb") as handle:
                while True:
                    block = handle.read(chunk_size)
                    if not block:
                        break
                    hasher.update(block)
                    total += len(block)
                    chunks += 1
                    if total > expected_size:
                        raise ReleaseValidationError(
                            f"layer {digest} grew beyond descriptor size {expected_size}",
                            code="size_mismatch",
                        )
                    if chunk_delay:
                        time.sleep(chunk_delay)
        except OSError as exc:
            raise ReleaseValidationError(
                f"cannot read layer blob {digest}: {exc}", code="blob_unreadable"
            ) from exc

        if total != expected_size:
            raise ReleaseValidationError(
                f"layer {digest} is {total} bytes, descriptor says {expected_size}",
                code="size_mismatch",
            )
        computed = "sha256:" + hasher.hexdigest()
        if computed != digest:
            raise ReleaseValidationError(
                f"layer digest mismatch: descriptor {digest}, content {computed}",
                code="digest_mismatch",
            )
        return HashStats(bytes_hashed=total, chunks=chunks)


def _descriptor(desc: Any, *, what: str) -> tuple[str, int, str]:
    if not isinstance(desc, dict):
        raise ReleaseValidationError(f"{what} descriptor must be an object", code="invalid_manifest")
    digest = validate_digest(desc.get("digest"), what=f"{what} digest")
    size = desc.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ReleaseValidationError(
            f"{what} descriptor size must be a non-negative integer", code="invalid_descriptor"
        )
    media_type = desc.get("mediaType")
    if not isinstance(media_type, str) or not media_type:
        raise ReleaseValidationError(
            f"{what} descriptor mediaType must be a non-empty string",
            code="invalid_descriptor",
        )
    return digest, size, media_type


def verify_release(
    layout: OciLayout,
    settings: Settings,
    architecture: str,
    manifest_digest: str,
) -> VerifiedRelease:
    """Verify a candidate manifest and everything it references.

    This function performs no state mutation and holds no release lock; it can
    run concurrently for different architectures (or for the same one) while
    the stable index stays queryable.
    """
    started = time.monotonic()
    manifest_digest = validate_digest(manifest_digest, what="manifest digest")

    manifest_bytes = layout.read_blob(
        manifest_digest, max_bytes=MAX_MANIFEST_BYTES, what="manifest"
    )
    if digest_bytes(manifest_bytes) != manifest_digest:
        raise ReleaseValidationError(
            "manifest content does not match the submitted digest", code="digest_mismatch"
        )
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseValidationError(
            f"manifest is not valid JSON: {exc}", code="invalid_manifest"
        ) from exc
    if not isinstance(manifest, dict):
        raise ReleaseValidationError("manifest must be a JSON object", code="invalid_manifest")
    if manifest.get("schemaVersion") != 2:
        raise ReleaseValidationError(
            "manifest schemaVersion must be 2", code="invalid_manifest"
        )
    manifest_media_type = manifest.get("mediaType")
    if not isinstance(manifest_media_type, str) or not settings.media_type_allowed(
        manifest_media_type
    ):
        raise ReleaseValidationError(
            f"unsupported manifest mediaType: {manifest_media_type!r}",
            code="unsupported_media_type",
        )
    if not manifest_digest.startswith("sha256:"):
        raise ReleaseValidationError("only sha256 digests are supported", code="invalid_digest")

    config_digest, config_size, config_media_type = _descriptor(
        manifest.get("config"), what="config"
    )
    if config_media_type not in CONFIG_MEDIA_TYPES:
        raise ReleaseValidationError(
            f"unsupported config mediaType: {config_media_type}", code="unsupported_media_type"
        )
    if config_size > MAX_CONFIG_BYTES:
        raise ReleaseValidationError(
            f"config descriptor size {config_size} exceeds limit {MAX_CONFIG_BYTES}",
            code="blob_too_large",
        )
    config_bytes = layout.read_blob(
        config_digest, max_bytes=MAX_CONFIG_BYTES, what="config"
    )
    if len(config_bytes) != config_size:
        raise ReleaseValidationError(
            f"config descriptor size {config_size} does not match blob size {len(config_bytes)}",
            code="size_mismatch",
        )
    if digest_bytes(config_bytes) != config_digest:
        raise ReleaseValidationError(
            f"config digest mismatch: descriptor {config_digest}, content {digest_bytes(config_bytes)}",
            code="digest_mismatch",
        )
    try:
        config = json.loads(config_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseValidationError(
            f"config is not valid JSON: {exc}", code="invalid_manifest"
        ) from exc
    if not isinstance(config, dict):
        raise ReleaseValidationError("image config must be a JSON object", code="invalid_manifest")

    config_os = config.get("os")
    config_arch = config.get("architecture")
    if not isinstance(config_os, str) or not isinstance(config_arch, str):
        raise ReleaseValidationError(
            "image config must declare string 'os' and 'architecture'",
            code="invalid_manifest",
        )
    if config_os != "linux":
        raise ReleaseValidationError(
            f"image platform is {config_os}/{config_arch}; only linux is supported",
            code="platform_mismatch",
        )
    if config_arch != architecture:
        raise ReleaseValidationError(
            f"image config architecture is {config_arch}, requested {architecture}",
            code="platform_mismatch",
        )

    layers = manifest.get("layers")
    if not isinstance(layers, list) or not layers:
        raise ReleaseValidationError(
            "manifest must contain at least one layer", code="invalid_manifest"
        )

    layer_bytes_total = 0
    chunk_total = 0
    for position, layer in enumerate(layers):
        layer_digest, layer_size, layer_media_type = _descriptor(
            layer, what=f"layer[{position}]"
        )
        if layer_media_type not in LAYER_MEDIA_TYPES:
            raise ReleaseValidationError(
                f"layer[{position}] has unsupported mediaType: {layer_media_type}",
                code="unsupported_media_type",
            )
        stats = layout.hash_blob(
            layer_digest,
            expected_size=layer_size,
            chunk_size=settings.layer_chunk_bytes,
            chunk_delay=settings.layer_chunk_delay,
        )
        layer_bytes_total += stats.bytes_hashed
        chunk_total += stats.chunks

    platform: dict[str, str] = {"architecture": config_arch, "os": config_os}
    variant = config.get("variant")
    if isinstance(variant, str) and variant:
        platform["variant"] = variant

    descriptor: dict[str, Any] = {
        "mediaType": manifest_media_type,
        "digest": manifest_digest,
        "size": len(manifest_bytes),
        "platform": platform,
    }
    # Carry the human-facing version annotation through to the index descriptor
    # so operators can see which build is stable without resolving each manifest.
    manifest_annotations = manifest.get("annotations")
    if isinstance(manifest_annotations, dict):
        carried = {
            key: value
            for key, value in manifest_annotations.items()
            if key
            in (
                "org.opencontainers.image.version",
                "org.opencontainers.image.created",
                "org.opencontainers.image.revision",
                "org.opencontainers.image.title",
            )
            and isinstance(value, str)
        }
        if carried:
            descriptor["annotations"] = carried
    elapsed_ms = round((time.monotonic() - started) * 1000, 3)

    return VerifiedRelease(
        platform=f"linux/{architecture}",
        architecture=architecture,
        os="linux",
        manifest_digest=manifest_digest,
        descriptor=descriptor,
        config_bytes=len(config_bytes),
        layer_count=len(layers),
        layer_bytes=layer_bytes_total,
        chunks=chunk_total,
        elapsed_ms=elapsed_ms,
    )
