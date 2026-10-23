"""Which engine is running, which one the project pins, and whether they agree.

Two engines can share one `.dex/` without either knowing (#731). The skills build
the engine their launcher pins, while a project that also depends on dex directly
pins its own in `uv.lock` or `pyproject.toml`; both read and write the same cache
and baseline. Nothing here refuses, since running the launcher's engine against a
project that pins another is a legitimate thing to do. It is said, though, on
every envelope, and a stored document written by another release says so when it
is read.

Unknown is `None`, never a version. `0.0.0` is hatch-vcs's old fallback for a
source build with no `.git` (the installed plugin was exactly that build), so a
document stamped with it records no claim and is never compared.

Stdlib only, and never raises out of the public functions: a project file that
will not parse is a project with no pin dex can read, not a failed command.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

DISTRIBUTION = "exmergo-dex-core"

#: Stamps that record no version. `0.0.0` is the pre-#731 source-build fallback.
_UNKNOWN = frozenset({"", "0.0.0"})

# A requirement string, as far as a pin needs: the name, optional extras, and the
# specifier set up to any environment marker. A direct reference (`name @ url`)
# fails the specifier match and is therefore never a pin.
_REQUIREMENT = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(?P<spec>[^;]*)"
)
_EXACT = re.compile(r"^==\s*(?P<version>[A-Za-z0-9.+!_-]+)$")
# What `same_release` ignores: a dev segment and a local label, which every
# monorepo commit changes without changing what the engine reads or writes.
_DEV_OR_LOCAL = re.compile(r"(?:[.-]?dev\d*)?(?:\+.*)?$")


@dataclass(frozen=True)
class ProjectPin:
    """An exact engine version the project's own files pin, and which file said so."""

    version: str
    source: str


def _known(version: str | None) -> str | None:
    if version is None:
        return None
    version = version.strip()
    return None if version in _UNKNOWN else version


def running_version() -> str | None:
    """The version of the engine running this process, or None when unknown."""

    try:
        from importlib.metadata import version

        return _known(version(DISTRIBUTION))
    except Exception:
        return None


def _normalized_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def same_release(a: str | None, b: str | None) -> bool:
    """Whether two versions name the same release, or either names none.

    A dev segment and a local label are dropped first, so two commits of one
    unreleased version agree. An unknown side is agreement rather than a
    mismatch, because a stamp that records nothing cannot contradict anything.
    """

    a, b = _known(a), _known(b)
    if a is None or b is None:
        return True
    return _DEV_OR_LOCAL.sub("", a.lower()) == _DEV_OR_LOCAL.sub("", b.lower())


def _pin_from_requirement(requirement: object) -> str | None:
    if not isinstance(requirement, str):
        return None
    match = _REQUIREMENT.match(requirement)
    if match is None or _normalized_name(match["name"]) != DISTRIBUTION:
        return None
    exact = _EXACT.match(match["spec"].strip())
    if exact is None or "*" in exact["version"]:
        return None
    return exact["version"]


def _read_toml(path: Path) -> dict | None:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None


def _pin_from_lock(path: Path) -> str | None:
    """The registry release `uv.lock` resolves the engine to.

    Only a registry source is a pin. An editable, path, directory or git source is
    a checkout of dex itself (the monorepo's own lock is one), whose version the
    lock does not fix.
    """

    lock = _read_toml(path)
    if lock is None:
        return None
    for package in lock.get("package") or []:
        if not isinstance(package, dict):
            continue
        if _normalized_name(str(package.get("name", ""))) != DISTRIBUTION:
            continue
        source = package.get("source")
        if isinstance(source, dict) and "registry" in source:
            return _known(str(package.get("version", "")))
    return None


def _pin_from_pyproject(path: Path) -> str | None:
    """The exact `==` pin a pyproject.toml declares, wherever uv reads one from.

    A range is not a pin, and the package's own pyproject is not a dependent.
    """

    document = _read_toml(path)
    if document is None:
        return None
    project = document.get("project") or {}
    if _normalized_name(str(project.get("name", ""))) == DISTRIBUTION:
        return None
    candidates: list[object] = list(project.get("dependencies") or [])
    for group in (project.get("optional-dependencies") or {}).values():
        candidates.extend(group or [])
    for group in (document.get("dependency-groups") or {}).values():
        candidates.extend(group or [])
    uv = (document.get("tool") or {}).get("uv") or {}
    candidates.extend(uv.get("dev-dependencies") or [])
    for requirement in candidates:
        pinned = _pin_from_requirement(requirement)
        if pinned is not None:
            return _known(pinned)
    return None


def project_pin(root: Path) -> ProjectPin | None:
    """The engine version the Python project around `root` pins, or None.

    Walks up from `root` to the enclosing git repository, the ceiling every other
    dex walk-up uses (`command_args.resolve_dex_root`), and without one reads only
    `root`. The nearest directory holding a `uv.lock` or `pyproject.toml` is the
    project; its lock wins over its pyproject, because the lock is what the
    project's environment actually installs.
    """

    try:
        start = root.resolve()
        ceiling = start
        for directory in (start, *start.parents):
            if (directory / ".git").exists():
                ceiling = directory
                break
        for directory in (start, *start.parents):
            lock = directory / "uv.lock"
            pyproject = directory / "pyproject.toml"
            if lock.is_file() or pyproject.is_file():
                if lock.is_file() and (version := _pin_from_lock(lock)):
                    return ProjectPin(version, _display(lock, start))
                if pyproject.is_file() and (version := _pin_from_pyproject(pyproject)):
                    return ProjectPin(version, _display(pyproject, start))
                return None
            if directory == ceiling:
                break
    except OSError:
        return None
    return None


def _display(path: Path, start: Path) -> str:
    try:
        return str(path.relative_to(start))
    except ValueError:
        return str(path)


def pin_warning(root: Path, running: str | None = None) -> str | None:
    """One warning when the project pins a different engine than the one running."""

    running = _known(running) if running is not None else running_version()
    pin = project_pin(root)
    if pin is None or running is None or same_release(pin.version, running):
        return None
    return (
        f"this project pins {DISTRIBUTION} {pin.version} ({pin.source}), but the "
        f"engine running this command is {running}; both write the same .dex/ "
        "state. Align the project's pin with the version the dex skills run, or "
        "run the project's own engine (`uv run python -m exmergo_dex_core`)"
    )


def state_version_warning(
    document: str, recorded: str | None, remedy: str, running: str | None = None
) -> str | None:
    """A warning when a stored document was written by another engine release.

    `document` names it ("the drift baseline"), `remedy` is the sentence that
    follows. Silent when either side is unknown.
    """

    running = _known(running) if running is not None else running_version()
    recorded = _known(recorded)
    if recorded is None or running is None or same_release(recorded, running):
        return None
    return (
        f"{document} was written by dex {recorded} and this engine is {running}; "
        f"{remedy}"
    )
