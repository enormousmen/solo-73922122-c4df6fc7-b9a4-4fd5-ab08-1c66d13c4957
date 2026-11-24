"""Typed configuration loaded from environment variables.

Only the environment is consulted so that the same image can run under Docker
Compose with a read-only bind mount and no secrets on the command line.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_ARCHITECTURES: tuple[str, ...] = ("amd64", "arm64")


class ConfigError(ValueError):
    """Raised when the process environment is not usable."""


def _split(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    """Immutable runtime settings for the release service."""

    oci_layout: Path
    state_dir: Path
    index_ref: str = "registry.example.com/edge/stable:latest"
    architectures: tuple[str, ...] = DEFAULT_ARCHITECTURES
    layer_chunk_bytes: int = 64 * 1024
    layer_chunk_delay: float = 0.0
    allowed_media_types: tuple[str, ...] = field(
        default_factory=lambda: (
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json",
        )
    )

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "Settings":
        env = os.environ if environ is None else environ
        raw_layout = env.get("OCI_LAYOUT_DIR", "/oci-layout")
        raw_state = env.get("STATE_DIR", "/state")
        raw_archs = env.get("OCI_ARCHITECTURES", ",".join(DEFAULT_ARCHITECTURES))
        raw_ref = env.get("OCI_INDEX_REF", "registry.example.com/edge/stable:latest")
        raw_chunk = env.get("LAYER_CHUNK_BYTES", str(64 * 1024))
        raw_chunk_delay = env.get("LAYER_CHUNK_DELAY_MS", "0")

        architectures = tuple(_split(raw_archs))
        if not architectures:
            raise ConfigError("OCI_ARCHITECTURES must contain at least one architecture")
        for arch in architectures:
            if "/" in arch or not arch:
                raise ConfigError(f"invalid architecture name: {arch!r}")
        if len(set(architectures)) != len(architectures):
            raise ConfigError("OCI_ARCHITECTURES contains duplicates")

        try:
            chunk = int(raw_chunk)
        except ValueError as exc:
            raise ConfigError("LAYER_CHUNK_BYTES must be an integer") from exc
        if chunk < 1 or chunk > 16 * 1024 * 1024:
            raise ConfigError("LAYER_CHUNK_BYTES must be between 1 and 16777216")

        try:
            chunk_delay_ms = float(raw_chunk_delay)
        except ValueError as exc:
            raise ConfigError("LAYER_CHUNK_DELAY_MS must be numeric") from exc
        if not 0 <= chunk_delay_ms <= 1000:
            raise ConfigError("LAYER_CHUNK_DELAY_MS must be between 0 and 1000")

        if not raw_ref.strip() or any(c.isspace() for c in raw_ref):
            raise ConfigError("OCI_INDEX_REF must be a non-empty reference without spaces")

        return cls(
            oci_layout=Path(raw_layout),
            state_dir=Path(raw_state),
            index_ref=raw_ref.strip(),
            architectures=architectures,
            layer_chunk_bytes=chunk,
            layer_chunk_delay=chunk_delay_ms / 1000.0,
        )

    def media_type_allowed(self, media_type: object) -> bool:
        return isinstance(media_type, str) and media_type in self.allowed_media_types
