"""Release-manifest verification for injected executable components.

Runtime secrets and narrowly scoped authorization callables may be injected,
but an injected object cannot authenticate its own executable semantics with a
string identifier.  Every protocol implementation invoked by the runtime must
resolve to source bytes present in the already verified release inventory.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import inspect
from pathlib import Path
import re
from typing import Mapping, Sequence


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class UnsignedCompositionError(RuntimeError):
    def __init__(self, role: str, detail: str) -> None:
        self.role = str(role)
        self.detail = str(detail)
        super().__init__(f"UNSIGNED_COMPOSITION:{self.role}:{self.detail}")


@dataclass(frozen=True)
class ReleaseBoundComponentEvidence:
    role: str
    implementation: str
    release_manifest_hash: str
    source_files: tuple[tuple[str, str], ...]

    @property
    def evidence_hash(self) -> str:
        payload = "\n".join(
            (
                self.role,
                self.implementation,
                self.release_manifest_hash,
                *(f"{path}:{digest}" for path, digest in self.source_files),
            )
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _implementation_source(member: object) -> Path | None:
    if isinstance(member, property):
        member = member.fget
    elif isinstance(member, (classmethod, staticmethod)):
        member = member.__func__
    if member is None:
        return None
    try:
        value = inspect.getsourcefile(member) or inspect.getfile(member)
    except (TypeError, OSError):
        return None
    return Path(value).resolve()


def verify_release_bound_component(
    component: object,
    *,
    role: str,
    semantic_members: Sequence[str],
    manifest: Mapping[str, object],
    release_root: str | Path,
) -> ReleaseBoundComponentEvidence:
    """Prove the exact component class and protocol methods are inventoried."""

    normalized_role = str(role).strip()
    if not normalized_role:
        raise ValueError("component role is required")
    manifest_hash = str(manifest.get("release_manifest_hash", ""))
    if not _SHA256.fullmatch(manifest_hash):
        raise UnsignedCompositionError(normalized_role, "MANIFEST_HASH_INVALID")
    raw_files = manifest.get("files")
    if not isinstance(raw_files, list):
        raise UnsignedCompositionError(normalized_role, "MANIFEST_INVENTORY_MISSING")
    inventory: dict[str, tuple[str, int]] = {}
    for row in raw_files:
        if not isinstance(row, Mapping):
            raise UnsignedCompositionError(normalized_role, "MANIFEST_INVENTORY_INVALID")
        path = str(row.get("path", ""))
        digest = str(row.get("sha256", ""))
        size = row.get("size")
        if (
            not path
            or not _SHA256.fullmatch(digest)
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
        ):
            raise UnsignedCompositionError(normalized_role, "MANIFEST_INVENTORY_INVALID")
        inventory[path] = (digest, size)

    implementation_type = type(component)
    source_paths: set[Path] = set()
    if inspect.isfunction(component):
        # A provider calendar/session callback is executable composition too.
        # Only a plain, release-inventoried function is accepted: closures and
        # default-bound objects would otherwise smuggle unprofiled semantics
        # into an apparently release-bound callback.
        if tuple(semantic_members) not in ((), ("__call__",)):
            raise UnsignedCompositionError(
                normalized_role, "FUNCTION_SEMANTIC_MEMBERS_INVALID"
            )
        if component.__closure__ or component.__defaults__ or component.__kwdefaults__:
            raise UnsignedCompositionError(
                normalized_role, "FUNCTION_CAPTURE_OR_DEFAULT_NOT_ATTESTABLE"
            )
        function_source = _implementation_source(component)
        if function_source is None:
            raise UnsignedCompositionError(
                normalized_role, "IMPLEMENTATION_SOURCE_UNKNOWN"
            )
        source_paths.add(function_source)
        implementation = f"{component.__module__}.{component.__qualname__}"
    else:
        class_source = _implementation_source(implementation_type)
        if class_source is None:
            raise UnsignedCompositionError(
                normalized_role, "IMPLEMENTATION_SOURCE_UNKNOWN"
            )
        source_paths.add(class_source)
        for name in semantic_members:
            try:
                member = inspect.getattr_static(implementation_type, name)
            except AttributeError as exc:
                raise UnsignedCompositionError(
                    normalized_role, f"SEMANTIC_MEMBER_MISSING:{name}"
                ) from exc
            # Static lookup avoids invoking an injected __getattribute__,
            # property, or descriptor before its implementation is trusted.
            # A distinct instance value is an unprofiled executable override.
            try:
                instance_member = inspect.getattr_static(component, name)
            except AttributeError as exc:
                raise UnsignedCompositionError(
                    normalized_role, f"SEMANTIC_MEMBER_MISSING:{name}"
                ) from exc
            if instance_member is not member:
                raise UnsignedCompositionError(
                    normalized_role, f"INSTANCE_OVERRIDES_SEMANTIC_MEMBER:{name}"
                )
            source = _implementation_source(member)
            if source is None:
                raise UnsignedCompositionError(
                    normalized_role, f"SEMANTIC_MEMBER_SOURCE_UNKNOWN:{name}"
                )
            source_paths.add(source)
        implementation = (
            f"{implementation_type.__module__}.{implementation_type.__qualname__}"
        )

    root = Path(release_root).resolve()
    evidence_files: list[tuple[str, str]] = []
    for source in sorted(source_paths):
        try:
            relative = source.relative_to(root).as_posix()
        except ValueError as exc:
            raise UnsignedCompositionError(
                normalized_role, "IMPLEMENTATION_OUTSIDE_RELEASE"
            ) from exc
        expected = inventory.get(relative)
        if expected is None:
            raise UnsignedCompositionError(
                normalized_role, f"IMPLEMENTATION_NOT_IN_MANIFEST:{relative}"
            )
        try:
            data = source.read_bytes()
        except OSError as exc:
            raise UnsignedCompositionError(
                normalized_role, f"IMPLEMENTATION_UNREADABLE:{relative}"
            ) from exc
        digest = hashlib.sha256(data).hexdigest()
        if len(data) != expected[1] or digest != expected[0]:
            raise UnsignedCompositionError(
                normalized_role, f"IMPLEMENTATION_BYTES_DIFFER:{relative}"
            )
        evidence_files.append((relative, digest))

    return ReleaseBoundComponentEvidence(
        role=normalized_role,
        implementation=implementation,
        release_manifest_hash=manifest_hash,
        source_files=tuple(evidence_files),
    )


__all__ = [
    "ReleaseBoundComponentEvidence",
    "UnsignedCompositionError",
    "verify_release_bound_component",
]
