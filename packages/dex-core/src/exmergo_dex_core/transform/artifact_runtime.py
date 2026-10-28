"""Artifact-producer provenance and per-runtime artifact isolation (#862).

Dex's dbt runtime and a separately pinned project or CI runtime can share a
``target/`` directory. Incompatible dbt versions have left
``target/run/<project>/snapshots/<name>.sql`` as a directory that a later
version then opens as a file (``IsADirectoryError``).

The policy is runtime isolation, through :func:`resolve_artifact_dir` and
:func:`runtime_artifact_dir`. A clean shared ``target/`` is reused, which is
the same-runtime case and is unchanged. A collision isolates this runtime in a
sibling directory, or refuses when isolation is off. Neither choice deletes
caller-owned artifacts.

A caller that needs the artifacts at a chosen path passes that directory, or a
runtime key that resolves one. :func:`artifact_paths` is the one naming of
``run_results.json`` and ``manifest.json`` inside that directory, so the build
and the evidence read use the same files. A reader that names an expected
invocation id refuses results from a different invocation. Missing artifacts
stay missing. Neither case is a successful completion.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import DexError

#: Relative path under ``target/`` where the file-vs-directory collision has been
#: observed across dbt versions (snapshot compiled SQL).
_SNAPSHOT_RUN_GLOB = "run/*/snapshots"


@dataclass(frozen=True)
class ArtifactProducer:
    """Who wrote the artifacts this evidence is about.

    Paths are relative project labels only. Absolute home directories and
    credential-bearing strings are refused rather than redacted in place, so a
    caller cannot accidentally publish them by formatting this object.
    """

    dbt_version: str | None
    adapter_version: str | None
    project: str | None
    target: str | None
    artifact_directory: str
    invocation_id: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "dbt_version": self.dbt_version,
            "adapter_version": self.adapter_version,
            "project": self.project,
            "target": self.target,
            "artifact_directory": self.artifact_directory,
            "invocation_id": self.invocation_id,
        }


_UNSAFE_PATH = re.compile(r"(^|[/\\])(\.env|credentials|secrets)([/\\]|$)", re.I)


def sanitize_artifact_directory(path: Path | str, *, project_root: Path | str) -> str:
    """Return a path label safe for public evidence.

    Prefer a path relative to ``project_root``. Fall back to the final path
    component. Refuse credential-like segments.
    """

    root = Path(project_root).resolve()
    raw = Path(path)
    try:
        rel = raw.resolve().relative_to(root)
        label = rel.as_posix() or "target"
    except ValueError:
        label = raw.name or "target"
    if _UNSAFE_PATH.search(label) or label.startswith(".."):
        raise ValueError("artifact directory label is not publishable")
    return label


def artifact_producer(
    *,
    project_root: Path | str,
    target: str,
    artifact_directory: Path | str,
    metadata: dict[str, Any] | None = None,
    project_name: str | None = None,
    adapter_version: str | None = None,
) -> ArtifactProducer:
    """Build sanitized producer metadata from dbt run_results metadata + context."""

    meta = metadata or {}
    return ArtifactProducer(
        dbt_version=(str(meta["dbt_version"]) if meta.get("dbt_version") else None),
        adapter_version=(
            str(adapter_version)
            if adapter_version
            else (str(meta["adapter_version"]) if meta.get("adapter_version") else None)
        ),
        project=project_name,
        target=target,
        artifact_directory=sanitize_artifact_directory(
            artifact_directory, project_root=project_root
        ),
        invocation_id=(
            str(meta["invocation_id"]) if meta.get("invocation_id") else None
        ),
    )


@dataclass(frozen=True)
class CollisionFinding:
    """A path that one runtime left as a directory and another expects as a file."""

    path: str
    kind: str  # "directory_where_file_expected"


def find_snapshot_path_collisions(artifact_dir: Path | str) -> list[CollisionFinding]:
    """Detect snapshot compiled-SQL paths that are directories instead of files.

    Models the dbt 1.12.6 (directory) then 1.10.11 (file) collision without
    running either runtime.
    """

    root = Path(artifact_dir)
    if not root.is_dir():
        return []
    found: list[CollisionFinding] = []
    for snapshots_dir in root.glob(f"{_SNAPSHOT_RUN_GLOB}"):
        if not snapshots_dir.is_dir():
            continue
        # A compiled snapshot named ``foo.sql`` that is actually a directory.
        found.extend(
            CollisionFinding(
                path=child.as_posix(),
                kind="directory_where_file_expected",
            )
            for child in snapshots_dir.iterdir()
            if child.name.endswith(".sql") and child.is_dir()
        )
    return found


def runtime_artifact_dir(base: Path | str, runtime_key: str) -> Path:
    """Per-runtime artifact directory under ``base`` (no deletion of siblings)."""

    key = re.sub(r"[^A-Za-z0-9._-]+", "_", runtime_key.strip()) or "runtime"
    return Path(base) / f"dex-runtime-{key}"


class ArtifactCollisionError(DexError, ValueError):
    """Shared artifact dir has a cross-runtime file/directory collision."""


def resolve_artifact_dir(
    shared: Path | str,
    runtime_key: str,
    *,
    isolate_on_collision: bool = True,
) -> Path:
    """Choose a writable artifact directory for this runtime.

    Same-runtime reuse of ``shared`` is fine when no collision is present.
    On collision, either return an isolated sibling directory (default) or
    refuse. Never recursively deletes caller-owned artifacts.
    """

    shared_path = Path(shared)
    collisions = find_snapshot_path_collisions(shared_path)
    if not collisions:
        shared_path.mkdir(parents=True, exist_ok=True)
        return shared_path
    if not isolate_on_collision:
        names = ", ".join(c.path for c in collisions[:5])
        raise ArtifactCollisionError(
            "incompatible artifacts in shared directory "
            f"(directory where file expected: {names}); "
            "use an isolated artifact directory per runtime"
        )
    isolated = runtime_artifact_dir(shared_path.parent, runtime_key)
    isolated.mkdir(parents=True, exist_ok=True)
    return isolated


@dataclass(frozen=True)
class ArtifactPaths:
    """The artifact files dex reads back from one directory.

    ``directory`` is that directory. ``run_results`` and ``manifest`` are the
    paths inside it, whether or not the files exist yet.
    """

    directory: Path
    run_results: Path
    manifest: Path


def artifact_paths(artifact_dir: Path | str) -> ArtifactPaths:
    """Name ``run_results.json`` and ``manifest.json`` inside ``artifact_dir``."""

    directory = Path(artifact_dir)
    return ArtifactPaths(
        directory=directory,
        run_results=directory / "run_results.json",
        manifest=directory / "manifest.json",
    )


def prepare_artifact_dir(
    project_root: Path | str,
    runtime_key: str,
    *,
    shared_relative: str = "target",
    isolate_on_collision: bool = True,
) -> Path:
    """Resolve the artifact directory for ``runtime_key`` in this project.

    ``shared_relative`` is the caller-owned directory, ``target`` by default.
    The choice is :func:`resolve_artifact_dir`. A clean shared directory is
    reused. A collision isolates or refuses. Files already in the shared
    directory are left where they are.
    """

    return resolve_artifact_dir(
        Path(project_root) / shared_relative,
        runtime_key,
        isolate_on_collision=isolate_on_collision,
    )


def clear_run_results(artifact_dir: Path | str) -> None:
    """Unlink ``run_results.json`` in this directory and nothing else.

    A missing file is a no-op, so a first build and a directory that was never
    written both pass. The rest of the directory, including a previous
    runtime's collision, stays.
    """

    artifact_paths(artifact_dir).run_results.unlink(missing_ok=True)


class ForeignInvocationError(DexError, ValueError):
    """``run_results.json`` belongs to a different invocation than expected."""


def assert_same_invocation(expected_id: str | None, actual_id: str | None) -> None:
    """Raise when both invocation ids are present and they differ.

    A missing artifact has no id, and a reader that did not name one is not
    checking. Neither of those is a foreign invocation. Two different ids are,
    and a successful completion must not be returned from them.
    """

    if expected_id is None or actual_id is None or expected_id == actual_id:
        return
    raise ForeignInvocationError(
        f"run results belong to invocation '{actual_id}', not '{expected_id}'"
    )
