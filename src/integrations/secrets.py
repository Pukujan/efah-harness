"""Secret reference resolution.

Contract Section 6 requires ``secrets.refs.yaml`` to hold *references only*. This
adapter turns a reference into a value at call time and never writes one back.

DEC-003: the pack declares ``env:GITHUB_APP_PRIVATE_KEY`` but the environment
supplies ``GITHUB_APP_PRIVATE_KEY_PATH`` pointing at a PEM file. Contract Section
7.1 forbids raising an owner question for a discoverable fact, so the resolver
accepts both forms. An explicit value wins over a path when both are present, so
the owner can later export the variable directly with no code change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class MissingRequiredCredential(RuntimeError):
    """Typed blocker ``MISSING_REQUIRED_CREDENTIAL`` (contract Section 10.7)."""

    def __init__(self, ref_name: str, reference: str) -> None:
        self.ref_name = ref_name
        self.reference = reference
        super().__init__(
            f"MISSING_REQUIRED_CREDENTIAL: {ref_name} could not be resolved from {reference!r}"
        )


@dataclass(frozen=True)
class SecretRef:
    name: str
    reference: str
    required: bool = True


class SecretResolver:
    """Resolves ``env:NAME`` and ``file:/path`` references.

    Resolution order for ``env:NAME``:

    1. ``$NAME`` -- an inline value.
    2. ``$NAME_PATH`` -- a path whose file *contents* are the value (DEC-003).
    3. ``$NAME_FILE`` -- the same convention under its other common spelling.

    A value is never logged, never returned in a repr, and never persisted.
    """

    def __init__(self, environ: dict[str, str] | None = None) -> None:
        self._environ = environ if environ is not None else dict(os.environ)

    def resolve(self, ref: SecretRef) -> str | None:
        value = self._resolve_raw(ref.reference)
        if value is None and ref.required:
            raise MissingRequiredCredential(ref.name, ref.reference)
        return value

    def is_available(self, ref: SecretRef) -> bool:
        return self._resolve_raw(ref.reference) is not None

    def _resolve_raw(self, reference: str) -> str | None:
        scheme, _, target = reference.partition(":")
        if scheme == "env":
            return self._from_env(target)
        if scheme == "file":
            return self._read_file(Path(target))
        # A bare name is treated as an environment variable; the pack's
        # resolution_backend is `env`.
        return self._from_env(reference)

    def _from_env(self, name: str) -> str | None:
        direct = self._environ.get(name)
        if direct:
            return direct
        for suffix in ("_PATH", "_FILE"):
            candidate = self._environ.get(f"{name}{suffix}")
            if candidate:
                contents = self._read_file(Path(candidate))
                if contents:
                    return contents
        return None

    @staticmethod
    def _read_file(path: Path) -> str | None:
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            return None
        return text.strip() or None
