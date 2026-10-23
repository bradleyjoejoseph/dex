"""Which engine a project pins, and when two releases disagree (#731).

A project that depends on dex itself pins an engine in `uv.lock` or
`pyproject.toml`, while the skills run whichever one their launcher pins. Both
write the same `.dex/`, so `engine_version` reads the project's pin and compares
releases. These are the reading rules: what counts as a pin, which file wins,
how far the walk goes, and which versions are no claim at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exmergo_dex_core import engine_version
from exmergo_dex_core.engine_version import (
    ProjectPin,
    pin_warning,
    project_pin,
    running_version,
    same_release,
    state_version_warning,
)

REGISTRY_LOCK = """\
version = 1
requires-python = ">=3.11"

[[package]]
name = "exmergo-dex-core"
version = "{version}"
source = {{ registry = "https://pypi.org/simple" }}
"""

EDITABLE_LOCK = """\
version = 1

[[package]]
name = "exmergo-dex-core"
source = { editable = "." }
"""


def _write(root: Path, name: str, text: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(text, encoding="utf-8")


def _pyproject(*requirements: str, name: str = "growth") -> str:
    listed = ", ".join(f'"{r}"' for r in requirements)
    return f'[project]\nname = "{name}"\nversion = "0.1.0"\ndependencies = [{listed}]\n'


def test_a_registry_lock_entry_is_the_pin(tmp_path):
    _write(tmp_path, "uv.lock", REGISTRY_LOCK.format(version="1.14.0"))

    assert project_pin(tmp_path) == ProjectPin("1.14.0", "uv.lock")


def test_an_editable_lock_entry_is_a_checkout_not_a_pin(tmp_path):
    """The dex monorepo's own lock resolves the package as editable, and a
    checkout's version is whatever it builds to, not something the lock fixes."""

    _write(tmp_path, "uv.lock", EDITABLE_LOCK)

    assert project_pin(tmp_path) is None


@pytest.mark.parametrize(
    "requirement",
    [
        "exmergo-dex-core==1.14.0",
        "exmergo-dex-core[duckdb]==1.14.0",
        "exmergo_dex_core[duckdb] == 1.14.0",
        "Exmergo.Dex-Core==1.14.0; python_version >= '3.11'",
    ],
)
def test_an_exact_pyproject_requirement_is_the_pin(tmp_path, requirement):
    _write(tmp_path, "pyproject.toml", _pyproject(requirement))

    assert project_pin(tmp_path) == ProjectPin("1.14.0", "pyproject.toml")


@pytest.mark.parametrize(
    "requirement",
    [
        "exmergo-dex-core>=1.14",
        "exmergo-dex-core~=1.14.0",
        "exmergo-dex-core==1.14.*",
        "exmergo-dex-core",
        "exmergo-dex-core @ file:///somewhere/packages/dex-core",
        "exmergo-dex-core-extras==1.14.0",
    ],
)
def test_a_range_or_another_package_is_not_a_pin(tmp_path, requirement):
    _write(tmp_path, "pyproject.toml", _pyproject(requirement))

    assert project_pin(tmp_path) is None


def test_dependency_groups_and_uv_dev_dependencies_are_read(tmp_path):
    _write(
        tmp_path,
        "pyproject.toml",
        '[project]\nname = "growth"\nversion = "0.1.0"\n\n'
        '[dependency-groups]\ndev = ["exmergo-dex-core[duckdb]==1.13.2"]\n',
    )
    assert project_pin(tmp_path) == ProjectPin("1.13.2", "pyproject.toml")

    _write(
        tmp_path,
        "pyproject.toml",
        '[project]\nname = "growth"\nversion = "0.1.0"\n\n'
        '[tool.uv]\ndev-dependencies = ["exmergo-dex-core==1.13.1"]\n',
    )
    assert project_pin(tmp_path) == ProjectPin("1.13.1", "pyproject.toml")


def test_the_package_s_own_pyproject_is_not_a_dependent(tmp_path):
    _write(
        tmp_path,
        "pyproject.toml",
        _pyproject("exmergo-dex-core==1.0.0", name="exmergo-dex-core"),
    )

    assert project_pin(tmp_path) is None


def test_the_lock_wins_over_the_pyproject(tmp_path):
    """The lock is what the project's environment installs; a pyproject pin the
    lock has not caught up with is the less accurate of the two."""

    _write(tmp_path, "pyproject.toml", _pyproject("exmergo-dex-core==1.13.0"))
    _write(tmp_path, "uv.lock", REGISTRY_LOCK.format(version="1.14.0"))

    assert project_pin(tmp_path) == ProjectPin("1.14.0", "uv.lock")


def test_the_walk_finds_the_project_above_the_dex_root(tmp_path):
    (tmp_path / ".git").mkdir()
    _write(tmp_path, "uv.lock", REGISTRY_LOCK.format(version="1.14.0"))
    nested = tmp_path / "analytics"
    nested.mkdir()

    pin = project_pin(nested)

    assert pin is not None and pin.version == "1.14.0"


def test_the_walk_stops_at_the_git_ceiling(tmp_path):
    _write(tmp_path, "uv.lock", REGISTRY_LOCK.format(version="1.14.0"))
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    assert project_pin(repo) is None


def test_the_nearest_project_decides_even_when_it_pins_nothing(tmp_path):
    """A nested Python project with no pin is the project, not a doorway to a
    parent's pin, so the parent's lock is never borrowed."""

    (tmp_path / ".git").mkdir()
    _write(tmp_path, "uv.lock", REGISTRY_LOCK.format(version="1.14.0"))
    nested = tmp_path / "service"
    _write(nested, "pyproject.toml", _pyproject("requests==2.32.0"))

    assert project_pin(nested) is None


@pytest.mark.parametrize("name", ["uv.lock", "pyproject.toml"])
def test_a_file_that_will_not_parse_is_no_pin(tmp_path, name):
    _write(tmp_path, name, "this is [not toml")

    assert project_pin(tmp_path) is None


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("1.15.2", "1.15.2", True),
        ("1.15.2", "1.14.0", False),
        ("1.15.3.dev4+g943c8e0", "1.15.3.dev9+gabcdef0.d20261021", True),
        ("1.15.3.dev4", "1.15.2", False),
        ("1.15.0rc1", "1.15.0", False),
        ("0.0.0", "1.15.2", True),
        (None, "1.15.2", True),
        ("1.15.2", None, True),
    ],
)
def test_same_release(a, b, same):
    assert same_release(a, b) is same


def test_an_unknown_running_version_is_none_not_a_placeholder(monkeypatch):
    """`0.0.0` is the fallback a build with no `.git` used to report, so it must
    never be stamped as though it were a release."""

    import importlib.metadata

    monkeypatch.setattr(importlib.metadata, "version", lambda _name: "0.0.0")
    assert running_version() is None

    monkeypatch.setattr(importlib.metadata, "version", lambda _name: "1.15.2")
    assert running_version() == "1.15.2"


def test_the_pin_warning_names_both_versions_and_the_file(tmp_path):
    _write(tmp_path, "uv.lock", REGISTRY_LOCK.format(version="1.14.0"))

    warning = pin_warning(tmp_path, running="1.15.2")

    assert warning is not None
    assert "1.14.0" in warning and "1.15.2" in warning and "uv.lock" in warning


def test_no_pin_warning_when_the_releases_agree_or_nothing_is_pinned(tmp_path):
    assert pin_warning(tmp_path, running="1.15.2") is None

    _write(tmp_path, "uv.lock", REGISTRY_LOCK.format(version="1.15.2"))
    assert pin_warning(tmp_path, running="1.15.2") is None


def test_no_pin_warning_when_the_running_engine_is_unknown(tmp_path, monkeypatch):
    _write(tmp_path, "uv.lock", REGISTRY_LOCK.format(version="1.14.0"))
    monkeypatch.setattr(engine_version, "running_version", lambda: None)

    assert pin_warning(tmp_path) is None


def test_the_state_warning_is_silent_on_an_unrecorded_stamp():
    for recorded in (None, "0.0.0", "1.15.2"):
        assert (
            state_version_warning("the drift baseline", recorded, "fix", "1.15.2")
            is None
        )
    warning = state_version_warning("the drift baseline", "1.14.0", "fix it", "1.15.2")
    assert warning == (
        "the drift baseline was written by dex 1.14.0 and this engine is 1.15.2; fix it"
    )
